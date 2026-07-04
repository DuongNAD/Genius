import ast
import os
import pathspec
import tiktoken
from typing import Dict, List, Optional, Tuple


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

    def chunk_files(
        self, files: Dict[str, str], split_oversized: bool = False
    ) -> List[Dict[str, str]]:
        """Greedily groups files into chunks without exceeding token limit.

        ``split_oversized`` is opt-in: when True, a single file that exceeds
        the limit is split into structure-aware pieces (see split_file) keyed
        ``<path>#chunk<N>``; when False (the default, and the pinned legacy
        behavior) such a file is isolated whole in its own chunk.
        """
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
                if split_oversized:
                    pieces = self.split_file(filepath, content)
                    if len(pieces) > 1:
                        for i, piece in enumerate(pieces, 1):
                            chunks.append({f"{filepath}#chunk{i}": piece})
                        continue
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

    # --- structure-aware splitting of one oversized file ----------------
    # cAST-style ("Chunking via Abstract Syntax Trees", 2025) split-then-
    # merge: recursively split along AST statement boundaries — descending
    # into an oversized class/function body rather than cutting mid-
    # construct — then greedily merge adjacent small pieces back up toward
    # max_tokens. Pieces are contiguous keepends line slices, so
    # "".join(split_file(...)) reproduces the file byte-for-byte.

    def split_file(self, filepath: str, content: str) -> List[str]:
        """Chunks for ONE file. Python splits along AST boundaries; other
        content (or unparsable Python) falls back to token-bounded line
        windows. Files already within max_tokens come back whole."""
        if not content or self.count_tokens(content) <= self.max_tokens:
            return [content]
        lines = content.splitlines(keepends=True)
        if not lines:
            return [content]
        if filepath.endswith(".py"):
            try:
                tree = ast.parse(content)
            except (SyntaxError, ValueError):
                return self._piece_texts(
                    lines, self._window_ranges(lines, 1, len(lines))
                )
            pieces: List[Tuple[int, int]] = []
            for s, e, node in self._child_spans(tree, 1, len(lines)):
                pieces.extend(self._split_span(lines, s, e, node))
            return self._piece_texts(lines, self._merge_ranges(lines, pieces))
        return self._piece_texts(lines, self._window_ranges(lines, 1, len(lines)))

    def _child_spans(self, node, region_start: int, region_end: int):
        """Contiguous (start, end, child) line spans covering the region,
        one per direct child statement. Gap lines (comments/blanks) attach
        to the preceding span; the leading gap (or a class/def header when
        descending) attaches to the first child."""
        body = getattr(node, "body", None) or []
        if not body:
            return []
        starts = []
        for child in body:
            s = getattr(child, "lineno", region_start)
            decorators = getattr(child, "decorator_list", None)
            if decorators:
                s = min(s, decorators[0].lineno)
            starts.append(s)
        spans = []
        for i, child in enumerate(body):
            s = region_start if i == 0 else max(starts[i], spans[-1][1] + 1)
            e = region_end if i + 1 == len(body) else max(s, starts[i + 1] - 1)
            spans.append((s, e, child))
        return spans

    def _split_span(self, lines, start: int, end: int, node):
        """(start, end) pieces for one statement span, each within
        max_tokens where a finer boundary exists."""
        text = "".join(lines[start - 1 : end])
        if self.count_tokens(text) <= self.max_tokens:
            return [(start, end)]
        child_spans = self._child_spans(node, start, end)
        if child_spans:
            pieces = []
            for s, e, child in child_spans:
                pieces.extend(self._split_span(lines, s, e, child))
            return pieces
        # A single statement too big for the budget (huge literal, etc.):
        # no structural boundary left, cut into token-bounded line windows.
        return self._window_ranges(lines, start, end)

    def _window_ranges(self, lines, start: int, end: int):
        """Token-bounded contiguous line windows over lines[start..end]."""
        ranges = []
        window_start = start
        window_tokens = 0
        for i in range(start, end + 1):
            line_tokens = self.count_tokens(lines[i - 1])
            if window_tokens and window_tokens + line_tokens > self.max_tokens:
                ranges.append((window_start, i - 1))
                window_start = i
                window_tokens = line_tokens
            else:
                window_tokens += line_tokens
        ranges.append((window_start, end))
        return ranges

    def _merge_ranges(self, lines, pieces):
        """Greedily merge adjacent piece ranges while they fit max_tokens
        (the cAST merge pass, so many tiny statements share a chunk)."""
        merged = []
        cur = None
        cur_tokens = 0
        for s, e in pieces:
            t = self.count_tokens("".join(lines[s - 1 : e]))
            if cur is None:
                cur, cur_tokens = (s, e), t
            elif cur_tokens + t <= self.max_tokens:
                cur = (cur[0], e)
                cur_tokens += t
            else:
                merged.append(cur)
                cur, cur_tokens = (s, e), t
        if cur is not None:
            merged.append(cur)
        return merged

    @staticmethod
    def _piece_texts(lines, ranges) -> List[str]:
        return ["".join(lines[s - 1 : e]) for s, e in ranges]
