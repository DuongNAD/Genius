"""Tests for cAST-style structure-aware chunking (R4).

ProjectChunker.split_file: split-then-merge along AST boundaries, lossless
line slices. chunk_files keeps its pinned legacy behavior unless
split_oversized=True is passed.
"""

import ast

from ag_core.scanner.project_scanner import ProjectChunker


def _func(name: str, n_lines: int) -> str:
    body = "\n".join(f"    x{i} = {i}" for i in range(n_lines))
    return f"def {name}():\n{body}\n    return x0\n"


def test_split_file_under_limit_returns_whole():
    chunker = ProjectChunker(max_tokens=8000)
    src = _func("small", 3)
    assert chunker.split_file("m.py", src) == [src]


def test_split_file_python_boundaries_lossless():
    chunker = ProjectChunker(max_tokens=120)
    src = (
        "import os\n\n" + _func("f1", 8) + "\n" + _func("f2", 8) + "\n" + _func("f3", 8)
    )
    pieces = chunker.split_file("m.py", src)
    assert len(pieces) > 1
    assert "".join(pieces) == src  # lossless reconstruction
    for piece in pieces:  # cuts land on top-level statement boundaries
        ast.parse(piece)


def test_split_file_recurses_into_oversized_class():
    chunker = ProjectChunker(max_tokens=120)
    methods = "\n".join(
        f"    def m{i}(self):\n" + "\n".join(f"        y{j} = {j}" for j in range(12))
        for i in range(3)
    )
    src = f"class Big:\n{methods}\n"
    pieces = chunker.split_file("m.py", src)
    assert len(pieces) > 1
    assert "".join(pieces) == src
    assert pieces[0].startswith("class Big:")  # header rides with first child
    assert any("def m2" in piece for piece in pieces[1:])


def test_split_file_window_fallback_non_python_and_syntax_error():
    chunker = ProjectChunker(max_tokens=50)
    text = "\n".join(f"line {i} of plain text content" for i in range(60)) + "\n"
    pieces = chunker.split_file("notes.txt", text)
    assert len(pieces) > 1
    assert "".join(pieces) == text
    broken = "def broken(:\n" + "\n".join(f"junk {i}" for i in range(200)) + "\n"
    pieces = chunker.split_file("bad.py", broken)
    assert len(pieces) > 1
    assert "".join(pieces) == broken


def test_chunk_files_default_isolates_oversized_unchanged():
    chunker = ProjectChunker(max_tokens=100)
    big = _func("huge", 200)
    chunks = chunker.chunk_files({"big.py": big})
    assert chunks == [{"big.py": big}]  # pinned legacy behavior


def test_chunk_files_split_oversized_opt_in():
    chunker = ProjectChunker(max_tokens=120)
    big = "import os\n\n" + _func("f1", 8) + "\n" + _func("f2", 8)
    small = "print('hi')\n"
    chunks = chunker.chunk_files(
        {"small.py": small, "big.py": big}, split_oversized=True
    )
    flat = {}
    for chunk in chunks:
        flat.update(chunk)
    assert flat["small.py"] == small
    part_keys = sorted(
        (k for k in flat if k.startswith("big.py#chunk")),
        key=lambda k: int(k.rsplit("chunk", 1)[1]),
    )
    assert len(part_keys) > 1
    assert "".join(flat[k] for k in part_keys) == big
