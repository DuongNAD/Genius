import os
import pytest
from unittest.mock import patch, MagicMock

# Import the code to be tested
from ag_core.scanner.project_scanner import (
    is_binary_file,
    ProjectScanner,
    ProjectChunker,
)
from ag_core.utils.logger import calculate_usage_cost, log_transaction, logger


def test_is_binary_file(tmp_path):
    # Create a text file
    txt_file = tmp_path / "test.txt"
    txt_file.write_text("Hello, this is a normal text file.", encoding="utf-8")
    assert not is_binary_file(str(txt_file))

    # Create a binary file with null bytes in the first 1KB
    bin_file = tmp_path / "test.bin"
    bin_content = b"Some data\x00more data"
    bin_file.write_bytes(bin_content)
    assert is_binary_file(str(bin_file))

    # Create a larger binary file where null byte is later (e.g., at 500 bytes) but still in the first 1KB
    large_bin_file = tmp_path / "large_test.bin"
    large_bin_content = b"a" * 500 + b"\x00" + b"b" * 1000
    large_bin_file.write_bytes(large_bin_content)
    assert is_binary_file(str(large_bin_file))

    # Non-existent file should return True (treated as binary/skip)
    assert is_binary_file("non_existent_file.xyz")

    # Directory should return True
    assert is_binary_file(str(tmp_path))


def test_project_scanner(tmp_path):
    # Setup test folder structure:
    # tmp_path/
    #   .git/
    #     config
    #   src/
    #     main.py
    #     helper.py
    #     icon.png (binary)
    #   node_modules/
    #     dep/
    #       index.js
    #   build/
    #     output.txt
    #   README.md
    #   .gitignore

    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("git config", encoding="utf-8")

    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "main.py").write_text("print('hello')", encoding="utf-8")
    (src_dir / "helper.py").write_text("def help(): pass", encoding="utf-8")
    (src_dir / "icon.png").write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    )  # null byte in first 1KB

    (tmp_path / "node_modules" / "dep").mkdir(parents=True)
    (tmp_path / "node_modules" / "dep" / "index.js").write_text(
        "console.log()", encoding="utf-8"
    )

    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "output.txt").write_text("build output", encoding="utf-8")

    (tmp_path / "README.md").write_text("Readme file", encoding="utf-8")

    # Write .gitignore
    (tmp_path / ".gitignore").write_text("build/\n*.log\n", encoding="utf-8")
    (src_dir / "debug.log").write_text("log message", encoding="utf-8")

    # Scanner with extra ignores
    scanner = ProjectScanner(
        root_dir=str(tmp_path), extra_ignores=["extra_ignored.txt"]
    )

    # Create extra ignored file
    (tmp_path / "extra_ignored.txt").write_text("extra", encoding="utf-8")

    files = scanner.scan()

    # Assert expected files are scanned
    assert "src/main.py" in files
    assert "src/helper.py" in files
    assert "README.md" in files

    # Assert expected contents
    assert files["src/main.py"] == "print('hello')"
    assert files["README.md"] == "Readme file"

    # Assert ignored files are NOT scanned
    assert ".git/config" not in files
    assert "node_modules/dep/index.js" not in files
    assert "build/output.txt" not in files
    assert "src/icon.png" not in files
    assert "src/debug.log" not in files
    assert "extra_ignored.txt" not in files


def test_project_chunker():
    mock_encoding = MagicMock()
    # Mock encoding: each character is 1 token to make calculation predictable
    mock_encoding.encode.side_effect = lambda text: [0] * len(text)

    with patch(
        "tiktoken.encoding_for_model", return_value=mock_encoding
    ) as mock_model_enc, patch(
        "tiktoken.get_encoding", return_value=mock_encoding
    ) as mock_get_enc:

        # gpt-4 model will be used
        chunker = ProjectChunker(model_name="gpt-4", max_tokens=100)
        assert mock_model_enc.called or mock_get_enc.called

        # Check token counting
        text = "hello"
        assert chunker.count_tokens(text) == 5

        # Check formatting file payload
        formatted = chunker.format_file_payload("test.py", "print('hi')")
        # "\n--- File: test.py ---\nprint('hi')\n"
        assert formatted == "\n--- File: test.py ---\nprint('hi')\n"
        assert chunker.count_tokens(formatted) == len(formatted)

        # Check greedy chunking
        # We want to package multiple files. Let's define files with predictable sizes.
        # For example, max_tokens = 50.
        # file1 payload length: 20
        # file2 payload length: 20
        # file3 payload length: 20
        # file1 + file2 total tokens = 40 <= 50 -> in chunk 1
        # file1 + file2 + file3 total tokens = 60 > 50 -> file3 goes to chunk 2
        chunker_small = ProjectChunker(model_name="gpt-4", max_tokens=50)

        # Let's mock format_file_payload to control the return string size
        with patch.object(chunker_small, "format_file_payload") as mock_format:
            # Make the formatted content exactly of length 20
            mock_format.side_effect = lambda filepath, content: "x" * 20

            files = {
                "file1.py": "content1",
                "file2.py": "content2",
                "file3.py": "content3",
            }

            chunks = chunker_small.chunk_files(files)
            assert len(chunks) == 2
            assert chunks[0] == {"file1.py": "content1", "file2.py": "content2"}
            assert chunks[1] == {"file3.py": "content3"}


def test_project_chunker_large_file():
    mock_encoding = MagicMock()
    mock_encoding.encode.side_effect = lambda text: [0] * len(text)

    with patch("tiktoken.encoding_for_model", return_value=mock_encoding), patch(
        "tiktoken.get_encoding", return_value=mock_encoding
    ):

        chunker = ProjectChunker(model_name="gpt-4", max_tokens=50)

        # File 2 will be extremely large and exceed max_tokens (50)
        # File 1: 20 tokens
        # File 2: 60 tokens
        # File 3: 20 tokens

        def mock_format(filepath, content):
            if filepath == "file1.py":
                return "x" * 20
            elif filepath == "file2.py":
                return "x" * 60
            else:
                return "x" * 20

        with patch.object(chunker, "format_file_payload", side_effect=mock_format):
            files = {
                "file1.py": "content1",
                "file2.py": "content2",
                "file3.py": "content3",
            }
            chunks = chunker.chunk_files(files)

            # Expected behavior:
            # file1 (20) fits in current chunk.
            # file2 (60) exceeds max_tokens (50).
            #   current chunk (file1) is appended to chunks -> [ {file1} ]
            #   file2 is put in its own chunk -> [ {file1}, {file2} ]
            #   current chunk reset.
            # file3 (20) fits in new current chunk.
            # Loop ends, current chunk (file3) appended -> [ {file1}, {file2}, {file3} ]
            assert len(chunks) == 3
            assert chunks[0] == {"file1.py": "content1"}
            assert chunks[1] == {"file2.py": "content2"}
            assert chunks[2] == {"file3.py": "content3"}


def test_calculate_usage_cost():
    # Test gpt-4o: input 2.50, output 10.00 per 1M
    cost = calculate_usage_cost("gpt-4o", 1000000, 2000000)
    # 1.0 * 2.50 + 2.0 * 10.00 = 22.50
    assert cost == 22.50

    # Test case-insensitivity and substring matching
    cost_upper = calculate_usage_cost("GPT-4O-MINI-xyz", 1000000, 1000000)
    # gpt-4o-mini rates: input 0.15, output 0.60
    # 0.15 + 0.60 = 0.75
    assert abs(cost_upper - 0.75) < 1e-9

    # Test claude-3-5-sonnet: input 3.00, output 15.00
    cost_claude = calculate_usage_cost("claude-3-5-sonnet-20241022", 100000, 100000)
    # 0.1 * 3.00 + 0.1 * 15.00 = 0.30 + 1.50 = 1.80
    assert abs(cost_claude - 1.80) < 1e-9

    # Test fallback pricing: input 2.50, output 10.00
    cost_fallback = calculate_usage_cost("unknown-model", 1000000, 1000000)
    assert cost_fallback == 12.50


def test_log_transaction():
    with patch.object(logger, "info") as mock_info:
        log_transaction("gpt-4o", 1000, 2000)
        assert mock_info.call_count >= 4
        # Verify logger messages contain expected info
        messages = [call.args[0] for call in mock_info.call_args_list]
        combined = "".join(messages)
        assert "gpt-4o" in combined
        assert "1000" in combined
        assert "2000" in combined
        assert (
            "0.022500" in combined
        )  # (1000/1M)*2.50 + (2000/1M)*10.00 = 0.0025 + 0.02 = 0.0225
