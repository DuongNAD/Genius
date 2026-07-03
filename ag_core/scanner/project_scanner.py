import os
import pathspec
import tiktoken
from typing import Dict, List, Optional


def is_binary_file(filepath: str) -> bool:
    """Detects if a file is binary by looking for null bytes in the first 1KB."""
    try:
        if os.path.isdir(filepath):
            return True
        with open(filepath, "rb") as f:
            chunk = f.read(1024)
            return b"\x00" in chunk
    except Exception:
        return True


class ProjectScanner:
    def __init__(self, root_dir: str, extra_ignores: Optional[List[str]] = None):
        self.root_dir = os.path.abspath(root_dir)
        self.extra_ignores = extra_ignores or []
        self.spec = self._load_pathspec()

    def _load_pathspec(self) -> pathspec.PathSpec:
        """Loads .gitignore patterns and merges them with default and extra ignore patterns."""
        patterns: List[str] = [
            ".git/",
            "__pycache__/",
            "*.pyc",
            "*.pyo",
            "*.pyd",
            "node_modules/",
            "venv/",
            ".venv/",
            ".pytest_cache/",
        ]

        # Load .gitignore if present
        gitignore_path = os.path.join(self.root_dir, ".gitignore")
        if os.path.exists(gitignore_path):
            try:
                with open(gitignore_path, "r", encoding="utf-8") as f:
                    patterns.extend(f.read().splitlines())
            except Exception:
                pass

        # Append additional runtime ignores
        patterns.extend(self.extra_ignores)

        # Build pathspec matcher
        return pathspec.PathSpec.from_lines("gitignore", patterns)

    def scan(self) -> Dict[str, str]:
        """Scans the directory recursively and returns a map of relative paths to text contents."""
        scanned_files: Dict[str, str] = {}

        for dirpath, dirnames, filenames in os.walk(self.root_dir):
            # Compute relative directory path and standardize separators
            rel_dir = os.path.relpath(dirpath, self.root_dir)
            if rel_dir == ".":
                rel_dir = ""

            # Prune ignored subdirectories in-place to optimize traversal
            pruned_dirs = []
            for d in dirnames:
                if rel_dir:
                    rel_d = os.path.join(rel_dir, d).replace("\\", "/") + "/"
                else:
                    rel_d = d + "/"
                if not self.spec.match_file(rel_d):
                    pruned_dirs.append(d)
            dirnames[:] = (
                pruned_dirs  # Modifying this list in-place alters os.walk traversal
            )

            for filename in filenames:
                if rel_dir:
                    rel_file = os.path.join(rel_dir, filename).replace("\\", "/")
                else:
                    rel_file = filename

                # Check pathspec ignore rules
                if self.spec.match_file(rel_file):
                    continue

                abs_path = os.path.join(dirpath, filename)

                # One open per file (opens are costly on Windows): probe the
                # first 1KB for NUL bytes — same binary heuristic as
                # is_binary_file — then keep reading from the same handle.
                try:
                    with open(abs_path, "rb") as fh:
                        head = fh.read(1024)
                        if b"\x00" in head:
                            continue
                        data = head + fh.read()
                except Exception:
                    # Skip unreadable or locked files
                    continue
                text = data.decode("utf-8", errors="ignore")
                # Match the old text-mode read: universal-newline translation.
                if "\r" in text:
                    text = text.replace("\r\n", "\n").replace("\r", "\n")
                scanned_files[rel_file] = text

        return scanned_files


class ProjectChunker:
    def __init__(self, model_name: str = "gpt-4", max_tokens: int = 8000):
        self.model_name = model_name
        self.max_tokens = max_tokens
        self.encoding = self._get_encoding()

    def _get_encoding(self):
        """Loads specific model encoding or falls back to cl100k_base. Returns None if it fails."""
        try:
            return tiktoken.encoding_for_model(self.model_name)
        except Exception:
            try:
                return tiktoken.get_encoding("cl100k_base")
            except Exception:
                from ag_core.utils.logger import logger

                logger.warning(
                    "Failed to initialize tiktoken encoding, using fallback token estimator."
                )
                return None

    def count_tokens(self, text: str) -> int:
        if self.encoding is None:
            return len(text) // 4
        try:
            return len(self.encoding.encode(text))
        except Exception:
            return len(text) // 4

    def format_file_payload(self, filepath: str, content: str) -> str:
        """Formats file information using distinct header delimiters."""
        return f"\n--- File: {filepath} ---\n{content}\n"

    def chunk_files(self, files: Dict[str, str]) -> List[Dict[str, str]]:
        """Greedily groups files into chunks without exceeding token limit."""
        chunks: List[Dict[str, str]] = []
        current_chunk: Dict[str, str] = {}
        current_tokens = 0

        for filepath, content in files.items():
            formatted_text = self.format_file_payload(filepath, content)
            file_tokens = self.count_tokens(formatted_text)

            # If a single file exceeds the max tokens, isolate it to prevent lock-outs
            if file_tokens > self.max_tokens:
                if current_chunk:
                    chunks.append(current_chunk)
                    current_chunk = {}
                    current_tokens = 0
                chunks.append({filepath: content})
                continue

            if current_tokens + file_tokens > self.max_tokens:
                # Close the current chunk and initiate a new one
                chunks.append(current_chunk)
                current_chunk = {filepath: content}
                current_tokens = file_tokens
            else:
                current_chunk[filepath] = content
                current_tokens += file_tokens

        if current_chunk:
            chunks.append(current_chunk)

        return chunks
