#!/usr/bin/env python3
"""Print an ASCII file tree with per-file and per-directory code/comment line counts."""
import subprocess
import sys
import tokenize
from io import BytesIO
from pathlib import Path

# extension -> (line_comment_prefix, (block_start, block_end) or None)
COMMENT_STYLES: dict[str, tuple[str | None, tuple[str, str] | None]] = {
    ".py": ("#", None),
    ".pyi": ("#", None),
    ".sh": ("#", None),
    ".toml": ("#", None),
    ".yml": ("#", None),
    ".yaml": ("#", None),
    ".cfg": ("#", None),
    ".ini": ("#", None),
    ".rs": ("//", ("/*", "*/")),
    ".c": ("//", ("/*", "*/")),
    ".h": ("//", ("/*", "*/")),
    ".cpp": ("//", ("/*", "*/")),
    ".hpp": ("//", ("/*", "*/")),
    ".js": ("//", ("/*", "*/")),
    ".ts": ("//", ("/*", "*/")),
    ".lua": ("--", ("--[[", "]]")),
}

# Binary/media extensions: skip content analysis entirely (count is meaningless as LOC).
BINARY_EXTS = {".gif", ".webm", ".png", ".jpg", ".jpeg", ".ico", ".woff", ".woff2"}


def tracked_files(root: Path) -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=root, capture_output=True, text=True, check=True
    )
    return [root / line for line in out.stdout.splitlines() if line]


def read_text_lines(path: Path) -> list[str] | None:
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if text == "":
        return []
    return text.split("\n")[:-1] if text.endswith("\n") else text.split("\n")


def classify_python_lines(path: Path) -> tuple[int, int, int] | None:
    """Classify a .py/.pyi file with the stdlib tokenizer.

    This finds real `#` comments and ignores `#` inside strings, unlike the
    line-prefix heuristic used for other languages. Returns None if the file
    fails to tokenize (e.g. a syntax error), so the caller can fall back.
    """
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    try:
        tokens = list(tokenize.tokenize(BytesIO(data).readline))
    except (tokenize.TokenizeError, SyntaxError, IndentationError, ValueError):
        return None

    total_lines = data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
    comment_line_nums: set[int] = set()
    code_line_nums: set[int] = set()
    ignored = {
        tokenize.NEWLINE,
        tokenize.NL,
        tokenize.INDENT,
        tokenize.DEDENT,
        tokenize.ENCODING,
        tokenize.ENDMARKER,
        tokenize.COMMENT,
    }
    for tok in tokens:
        if tok.type == tokenize.COMMENT:
            comment_line_nums.add(tok.start[0])
        elif tok.type not in ignored:
            for line_num in range(tok.start[0], tok.end[0] + 1):
                code_line_nums.add(line_num)

    comment_only = comment_line_nums - code_line_nums
    code = len(code_line_nums)
    comment = len(comment_only)
    blank = max(0, total_lines - code - comment)
    return (code, comment, blank)


def classify_lines(path: Path) -> tuple[int, int, int]:
    """Return (code_lines, comment_lines, blank_lines)."""
    if path.suffix in BINARY_EXTS:
        return (0, 0, 0)

    if path.suffix in (".py", ".pyi"):
        result = classify_python_lines(path)
        if result is not None:
            return result

    raw_lines = read_text_lines(path)
    if raw_lines is None:
        return (0, 0, 0)

    line_prefix, block = COMMENT_STYLES.get(path.suffix, (None, None))
    code = comment = blank = 0
    in_block = False
    block_start, block_end = block if block else (None, None)

    for line in raw_lines:
        stripped = line.strip()
        if stripped == "":
            blank += 1
            continue
        if in_block:
            comment += 1
            if block_end and block_end in stripped:
                in_block = False
            continue
        if block_start and stripped.startswith(block_start):
            comment += 1
            if block_end not in stripped[len(block_start):]:
                in_block = True
            continue
        if line_prefix and stripped.startswith(line_prefix):
            comment += 1
            continue
        code += 1

    return (code, comment, blank)


class Node:
    def __init__(self, name: str):
        self.name = name
        self.children: dict[str, "Node"] = {}
        self.code = 0
        self.comment = 0
        self.is_file = False

    def totals(self) -> tuple[int, int]:
        if self.is_file:
            return (self.code, self.comment)
        code = comment = 0
        for child in self.children.values():
            c, m = child.totals()
            code += c
            comment += m
        return (code, comment)


def build_tree(root: Path, files: list[Path]) -> Node:
    tree = Node(root.name)
    for path in files:
        rel_parts = path.relative_to(root).parts
        node = tree
        for part in rel_parts[:-1]:
            node = node.children.setdefault(part, Node(part))
        leaf_name = rel_parts[-1]
        leaf = node.children.setdefault(leaf_name, Node(leaf_name))
        leaf.is_file = True
        leaf.code, leaf.comment, _blank = classify_lines(path)
    return tree


def format_counts(code: int, comment: int) -> str:
    return f"{code:>6} code {comment:>5} cm"


def render(node: Node, prefix: str, lines_out: list[str], width: int) -> None:
    entries = sorted(
        node.children.values(), key=lambda n: (n.is_file, n.name.lower())
    )
    for i, child in enumerate(entries):
        last = i == len(entries) - 1
        connector = "└── " if last else "├── "
        code, comment = child.totals()
        counts = format_counts(code, comment)
        label = child.name + ("/" if not child.is_file else "")
        pad = max(1, width - len(prefix) - len(connector) - len(label))
        lines_out.append(f"{prefix}{connector}{label}{' ' * pad}{counts}")
        if not child.is_file:
            extension = "    " if last else "│   "
            render(child, prefix + extension, lines_out, width)


def main() -> None:
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
    files = tracked_files(root)
    tree = build_tree(root, files)

    lines_out: list[str] = []
    code, comment = tree.totals()
    header = f"{root.name}/"
    lines_out.append(f"{header}{' ' * max(1, 70 - len(header))}{format_counts(code, comment)}")
    render(tree, "", lines_out, width=70)
    print("\n".join(lines_out))


if __name__ == "__main__":
    main()
