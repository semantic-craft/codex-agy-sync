from __future__ import annotations

import bisect
from dataclasses import dataclass
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tokenize

SKIP_DIRS: frozenset[str] = frozenset({
    ".git",
    "out",
    "target",
    "build",
    "dist",
    ".build",
    ".swiftpm",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".worktrees",
})

COMMENT_EXT_TO_LANG: dict[str, str] = {
    ".py": "python",
    ".rs": "rust",
    ".swift": "swift",
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
}

SHEBANG_PYTHON_RE = re.compile(r"^#!.*python")
SHEBANG_SHELL_RE = re.compile(r"^#!.*\b(?:sh|bash|zsh)\b")
CODING_COOKIE_RE = re.compile(r"^#\s*(?:-\*-\s*.*?\bcoding[:=]\s*[-\w.]+.*?-\*-|coding[:=]\s*[-\w.]+)")
SWIFT_TOOLS_RE = re.compile(r"^//\s*swift-tools-version\s*:")
LICENSE_RE = re.compile(
    r"SPDX-License-Identifier|Copyright|MIT License|Apache License|Licensed under",
    re.IGNORECASE,
)
DIRECTIVE_START_RE = re.compile(
    r"^(?:"
    r"swiftlint:"
    r"|swiftformat:"
    r"|swiftui-perf:"
    r"|noqa(?:\b|:)"
    r"|type:\s*ignore\b"
    r"|pyright:"
    r"|ruff:"
    r"|pragma:\s*no\s+cover\b"
    r"|clippy::"
    r"|SAFETY:"
    r"|shellcheck\s+disable\b"
    r"|notary-policy:"
    r")"
)
SHELL_HEREDOC_OP_RE = re.compile(
    r"""<<(-?)[ \t]*(?:'([^'\n]+)'|"([^"\n]+)"|\\?([A-Za-z_][A-Za-z0-9_.-]*))"""
)


@dataclass(frozen=True)
class RawComment:
    start_line: int
    end_line: int
    text: str
    inline: bool


class _LineIndex:
    def __init__(self, text: str) -> None:
        self.text = text
        self.lines = text.splitlines()
        self.starts = [0] + [idx + 1 for idx, ch in enumerate(text) if ch == "\n"]

    def make(self, start: int, end: int) -> RawComment:
        while end > start and self.text[end - 1] in ("\n", "\r"):
            end -= 1
        s_line = bisect.bisect_right(self.starts, start)
        col = start - self.starts[s_line - 1]
        e_line = bisect.bisect_right(self.starts, max(start, end - 1))
        e_col = end - self.starts[e_line - 1]
        before = self.lines[s_line - 1][:col].strip() if s_line <= len(self.lines) else ""
        after = self.lines[e_line - 1][e_col:].strip() if e_line <= len(self.lines) else ""
        return RawComment(s_line, e_line, self.text[start:end], bool(before or after))


def is_excluded_prefix(rel_posix: str, exclude_prefixes: list[str]) -> bool:
    for raw in exclude_prefixes:
        pref = raw.strip().replace("\\", "/").lstrip("./").lstrip("/")
        if not pref:
            continue
        if pref.endswith("/"):
            if rel_posix.startswith(pref):
                return True
        elif rel_posix == pref or rel_posix.startswith(pref + "/"):
            return True
    return False


def discover_repo_files(
    repo_root: Path,
    *,
    roots: tuple[str, ...] | None = None,
    suffix: str | None = None,
    exclude_prefixes: list[str] | None = None,
    use_git: bool = False,
) -> list[Path]:
    excludes = exclude_prefixes or []
    candidates: list[Path] | None = None
    if use_git and roots is None and (repo_root / ".git").exists():
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            capture_output=True,
            check=False,
        )
        if proc.returncode == 0:
            candidates = [
                Path(raw.decode("utf-8", errors="replace"))
                for raw in proc.stdout.split(b"\x00")
                if raw
            ]
    if candidates is None:
        candidates = []
        search_bases = [repo_root / r for r in roots] if roots is not None else [repo_root]
        for base in search_bases:
            if not base.is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
                dp = Path(dirpath)
                dirnames[:] = sorted(
                    d for d in dirnames
                    if d not in SKIP_DIRS and not d.endswith(".egg-info") and not (dp / d).is_symlink()
                )
                for fn in sorted(filenames):
                    full = dp / fn
                    if not full.is_symlink() and full.is_file():
                        candidates.append(full.relative_to(repo_root))

    accepted: list[Path] = []
    seen: set[str] = set()
    for rel in candidates:
        rel_posix = rel.as_posix()
        if rel_posix in seen:
            continue
        if suffix is not None and not rel_posix.endswith(suffix):
            continue
        if excludes and is_excluded_prefix(rel_posix, excludes):
            continue
        if any(part in SKIP_DIRS or part.endswith(".egg-info") for part in rel.parts[:-1]):
            continue
        full = repo_root / rel
        if full.is_symlink() or not full.is_file():
            continue
        seen.add(rel_posix)
        accepted.append(rel)
    return sorted(accepted, key=lambda p: p.as_posix())


def scan_swift_spans(text: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    comments: list[tuple[int, int]] = []
    strings: list[tuple[int, int]] = []
    n = len(text)
    i = 0
    interp_stack: list[tuple[bool, int, int]] = []
    if text.startswith("#!"):
        nl = text.find("\n")
        end = n if nl == -1 else nl
        comments.append((0, end))
        i = end

    def scan_string(multiline: bool, hashes: int, start_pos: int) -> None:
        nonlocal i
        closing = ('"""' if multiline else '"') + ("#" * hashes)
        escape = "\\" + ("#" * hashes)
        while i < n:
            if text.startswith(closing, i):
                end_pos = i + len(closing)
                strings.append((start_pos, end_pos))
                i = end_pos
                return
            if not multiline and text[i] == "\n":
                strings.append((start_pos, i))
                return
            if text.startswith(escape, i):
                after_esc = i + len(escape)
                if after_esc < n and text[after_esc] == "(":
                    strings.append((start_pos, after_esc + 1))
                    interp_stack.append((multiline, hashes, 1))
                    i = after_esc + 1
                    return
                i = min(n, after_esc + 1)
                continue
            i += 1
        strings.append((start_pos, n))

    while i < n:
        if text.startswith("//", i):
            nl = text.find("\n", i + 2)
            end = n if nl == -1 else nl
            comments.append((i, end))
            i = end
            continue
        if text.startswith("/*", i):
            start = i
            i += 2
            depth = 1
            while i < n and depth > 0:
                if text.startswith("/*", i):
                    depth += 1
                    i += 2
                elif text.startswith("*/", i):
                    depth -= 1
                    i += 2
                else:
                    i += 1
            comments.append((start, i))
            continue
        if text[i] == "#":
            j = i
            while j < n and text[j] == "#":
                j += 1
            hashes = j - i
            if j < n and text[j] == '"':
                start_q = i
                if text.startswith('"""', j):
                    i = j + 3
                    scan_string(True, hashes, start_q)
                else:
                    i = j + 1
                    scan_string(False, hashes, start_q)
                continue
            i = j
            continue
        if text[i] == '"':
            start_q = i
            if text.startswith('"""', i):
                i += 3
                scan_string(True, 0, start_q)
            else:
                i += 1
                scan_string(False, 0, start_q)
            continue
        if interp_stack:
            if text[i] == "(":
                ml, hs, depth = interp_stack[-1]
                interp_stack[-1] = (ml, hs, depth + 1)
                i += 1
                continue
            if text[i] == ")":
                ml, hs, depth = interp_stack[-1]
                if depth <= 1:
                    interp_stack.pop()
                    start_q = i
                    i += 1
                    scan_string(ml, hs, start_q)
                else:
                    interp_stack[-1] = (ml, hs, depth - 1)
                    i += 1
                continue
        i += 1
    return comments, strings


def mask_swift_source(text: str, *, mask_strings: bool) -> str:
    comments, strings = scan_swift_spans(text)
    out = list(text)
    spans = comments + (strings if mask_strings else [])
    for start, end in spans:
        for idx in range(start, end):
            if out[idx] != "\n":
                out[idx] = " "
    return "".join(out)


def _scan_quoted(text: str, pos: int, quote_ch: str, *, multiline: bool) -> int:
    n = len(text)
    i = pos + 1
    while i < n:
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == quote_ch:
            return i + 1
        if not multiline and ch == "\n":
            return i
        i += 1
    return n


def _lex_python_comments(text: str) -> list[RawComment]:
    lines = text.splitlines()
    out: list[RawComment] = []
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        if tok.type == tokenize.COMMENT:
            s_line, s_col = tok.start
            e_line, _ = tok.end
            before = lines[s_line - 1][:s_col].strip() if s_line <= len(lines) else ""
            out.append(RawComment(s_line, e_line, tok.string.rstrip("\r\n"), bool(before)))
    return out


def _match_rust_raw_string(text: str, pos: int) -> int | None:
    n = len(text)
    if pos > 0 and (text[pos - 1].isalnum() or text[pos - 1] == "_"):
        return None
    prefix_len = next((len(p) for p in ("br", "rb", "cr", "rc", "r") if text.startswith(p, pos)), 0)
    if prefix_len == 0:
        return None
    j = pos + prefix_len
    hashes = 0
    while j < n and text[j] == "#":
        hashes += 1
        j += 1
    if j >= n or text[j] != '"':
        return None
    closing = '"' + ("#" * hashes)
    end_idx = text.find(closing, j + 1)
    return n if end_idx == -1 else end_idx + len(closing)


def _scan_rust_char_or_lifetime(text: str, pos: int) -> int:
    n = len(text)
    if pos + 1 >= n:
        return pos + 1
    nxt = text[pos + 1]
    if nxt == "'":
        return pos + 2
    if nxt == "\\":
        j = pos + 2
        if j < n and text[j] != "\n":
            j += 1
        while j < n and text[j] not in ("'", "\n"):
            j += 1
        return j + 1 if (j < n and text[j] == "'") else pos + 1
    if nxt not in ("\n", "\r") and pos + 2 < n and text[pos + 2] == "'":
        return pos + 3
    return pos + 1


def _lex_rust_comments(text: str) -> list[RawComment]:
    idx = _LineIndex(text)
    out: list[RawComment] = []
    n = len(text)
    i = 0
    if text.startswith("#!") and not text.startswith("#!["):
        nl = text.find("\n")
        end = n if nl == -1 else nl
        out.append(idx.make(0, end))
        i = end
    while i < n:
        if text.startswith("//", i):
            nl = text.find("\n", i + 2)
            end = n if nl == -1 else nl
            out.append(idx.make(i, end))
            i = end
            continue
        if text.startswith("/*", i):
            start = i
            i += 2
            depth = 1
            while i < n and depth > 0:
                if text.startswith("/*", i):
                    depth += 1
                    i += 2
                elif text.startswith("*/", i):
                    depth -= 1
                    i += 2
                else:
                    i += 1
            out.append(idx.make(start, i))
            continue
        raw_end = _match_rust_raw_string(text, i)
        if raw_end is not None:
            i = raw_end
            continue
        prev_ident = i > 0 and (text[i - 1].isalnum() or text[i - 1] == "_")
        if not prev_ident and text[i : i + 2] in ('b"', 'c"'):
            i = _scan_quoted(text, i + 1, '"', multiline=True)
            continue
        if not prev_ident and text.startswith("b'", i):
            i = _scan_quoted(text, i + 1, "'", multiline=False)
            continue
        if text[i] == '"':
            i = _scan_quoted(text, i, '"', multiline=True)
            continue
        if text[i] == "'":
            i = _scan_rust_char_or_lifetime(text, i)
            continue
        i += 1
    return out


def _lex_swift_comments(text: str) -> list[RawComment]:
    idx = _LineIndex(text)
    comments, _ = scan_swift_spans(text)
    return [idx.make(start, end) for start, end in comments]


def _skip_shell_cmd_subst(text: str, pos: int) -> int:
    n = len(text)
    depth = 1
    i = pos
    while i < n and depth > 0:
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "'":
            end_q = text.find("'", i + 1)
            i = n if end_q == -1 else end_q + 1
            continue
        if ch == '"':
            i = _skip_shell_double_quoted(text, i + 1)
            continue
        if text.startswith("$(", i):
            depth += 1
            i += 2
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        i += 1
    return i


def _skip_shell_param_expansion(text: str, pos: int) -> int:
    n = len(text)
    depth = 1
    i = pos
    while i < n and depth > 0:
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "'":
            end_q = text.find("'", i + 1)
            i = n if end_q == -1 else end_q + 1
            continue
        if ch == '"':
            i = _skip_shell_double_quoted(text, i + 1)
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    return i


def _skip_shell_double_quoted(text: str, pos: int) -> int:
    n = len(text)
    i = pos
    while i < n:
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == '"':
            return i + 1
        if text.startswith("${", i):
            i = _skip_shell_param_expansion(text, i + 2)
            continue
        if text.startswith("$(", i):
            i = _skip_shell_cmd_subst(text, i + 2)
            continue
        i += 1
    return n


def _heredoc_closes_later(text: str, search_from: int, delim: str, strip_tabs: bool) -> bool:
    pos = search_from
    n = len(text)
    while pos < n:
        nl = text.find("\n", pos)
        end = n if nl == -1 else nl
        line = text[pos:end].rstrip("\r")
        if (line.lstrip("\t") if strip_tabs else line) == delim:
            return True
        if nl == -1:
            break
        pos = nl + 1
    return False


def _lex_shell_comments(text: str) -> list[RawComment]:
    idx = _LineIndex(text)
    out: list[RawComment] = []
    n = len(text)
    i = 0
    line_start = 0
    pending_heredocs: list[tuple[str, bool]] = []
    while i < n:
        ch = text[i]
        if ch == "\n":
            i += 1
            line_start = i
            while pending_heredocs and i < n:
                delim, strip_tabs = pending_heredocs.pop(0)
                while i < n:
                    nl = text.find("\n", i)
                    end = n if nl == -1 else nl
                    raw_line = text[i:end].rstrip("\r")
                    i = n if nl == -1 else nl + 1
                    line_start = i
                    if (raw_line.lstrip("\t") if strip_tabs else raw_line) == delim:
                        break
            continue
        if ch == "\\":
            i += 2
            if i <= n and text[i - 1] == "\n":
                line_start = i
            continue
        if ch == "'":
            end_q = text.find("'", i + 1)
            i = n if end_q == -1 else end_q + 1
            continue
        if ch == '"':
            i = _skip_shell_double_quoted(text, i + 1)
            continue
        if ch == "$":
            if i + 1 < n:
                nxt = text[i + 1]
                if nxt == "{":
                    i = _skip_shell_param_expansion(text, i + 2)
                    continue
                if nxt == "'":
                    i = _scan_quoted(text, i + 1, "'", multiline=True)
                    continue
                if nxt in ("#", "?", "$", "!", "*", "@", "-") or nxt.isdigit():
                    i += 2
                    continue
            i += 1
            continue
        if text.startswith("<<<", i):
            i += 3
            continue
        if text.startswith("<<", i):
            m = SHELL_HEREDOC_OP_RE.match(text, i)
            if m:
                strip_tabs = m.group(1) == "-"
                delim = m.group(2) or m.group(3) or m.group(4) or ""
                nl = text.find("\n", m.end())
                if delim and nl != -1 and _heredoc_closes_later(text, nl + 1, delim, strip_tabs):
                    pending_heredocs.append((delim, strip_tabs))
                    i = m.end()
                    continue
            i += 2
            continue
        if ch == "#" and (i == line_start or text[i - 1] in (" ", "\t", ";", "&", "|", "(", ")")):
            nl = text.find("\n", i)
            end = n if nl == -1 else nl
            out.append(idx.make(i, end))
            i = end
            continue
        i += 1
    return out


_COMMENT_LEXERS = {
    "python": _lex_python_comments,
    "rust": _lex_rust_comments,
    "swift": _lex_swift_comments,
    "shell": _lex_shell_comments,
}


def _comment_body(comment_text: str) -> str:
    stripped = comment_text.strip()
    if stripped.startswith("/*"):
        body = stripped[2:]
        if body.endswith("*/"):
            body = body[:-2]
        return body.lstrip(" \t*")
    if stripped.startswith("//"):
        return stripped[2:].lstrip()
    if stripped.startswith("#"):
        return stripped[1:].lstrip()
    return stripped


def is_exempt_comment(lang: str, comment: RawComment) -> bool:
    if comment.start_line == 1 and comment.text.startswith("#!"):
        return True
    if comment.start_line in (1, 2) and not comment.inline and CODING_COOKIE_RE.match(comment.text):
        return True
    if comment.start_line == 1 and lang == "swift" and SWIFT_TOOLS_RE.match(comment.text):
        return True
    if comment.start_line <= 30 and LICENSE_RE.search(comment.text):
        return True
    if not comment.inline and lang in ("rust", "swift"):
        if comment.text.startswith("///") and not comment.text.startswith("////"):
            return True
        if lang == "rust" and comment.text.startswith("//!"):
            return True
        if comment.text.startswith("/**") and len(comment.text) > 4 and comment.text[3] != "*":
            return True
    if DIRECTIVE_START_RE.match(_comment_body(comment.text)):
        return True
    return False


def _detect_comment_language(rel: Path, first_line: str) -> str | None:
    ext = rel.suffix.lower()
    if ext in COMMENT_EXT_TO_LANG:
        return COMMENT_EXT_TO_LANG[ext]
    if ext or not first_line.startswith("#!"):
        return None
    if SHEBANG_PYTHON_RE.match(first_line):
        return "python"
    if SHEBANG_SHELL_RE.match(first_line):
        return "shell"
    return None


def scan_comment_ratchet(repo_root: Path) -> tuple[list[str], int, int]:
    fix = "Delete restating comments or move non-obvious OS/protocol constraints to docs/DECISIONS.md and encode them in code/tests."
    cfg_path = repo_root / ".comment-ratchet.json"
    if not cfg_path.is_file():
        return (
            [
                "[COMMENT_RATCHET] .comment-ratchet.json:1: Missing .comment-ratchet.json configuration. "
                "Fix: Restore .comment-ratchet.json with max_total and exclude fields."
            ],
            0,
            0,
        )
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return (
            [
                f"[COMMENT_RATCHET] .comment-ratchet.json:1: Invalid .comment-ratchet.json ({exc}). "
                "Fix: Repair .comment-ratchet.json syntax."
            ],
            0,
            0,
        )
    max_total = int(cfg.get("max_total", 0))
    exclude_prefixes = [str(x) for x in cfg.get("exclude", [])]
    raw_file_limits = cfg.get("files") if isinstance(cfg.get("files"), dict) else {}
    file_limits: dict[str, int] = {str(k): int(v) for k, v in raw_file_limits.items()}

    ordinary_by_file: dict[str, list[RawComment]] = {}
    parse_errors: list[str] = []
    for rel in discover_repo_files(repo_root, exclude_prefixes=exclude_prefixes, use_git=True):
        full = repo_root / rel
        rel_posix = rel.as_posix()
        try:
            raw_bytes = full.read_bytes()
        except OSError as exc:
            parse_errors.append(
                f"[COMMENT_RATCHET] {rel_posix}:1: Unable to read file ({exc}). Fix: Ensure file is readable."
            )
            continue
        if b"\x00" in raw_bytes[:8192]:
            continue
        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            parse_errors.append(
                f"[COMMENT_RATCHET] {rel_posix}:1: Invalid UTF-8 encoding ({exc}). Fix: Save file as valid UTF-8."
            )
            continue
        first_line = text.splitlines()[0] if text else ""
        lang = _detect_comment_language(rel, first_line)
        if lang is None:
            continue
        try:
            raw_comments = _COMMENT_LEXERS[lang](text)
        except (tokenize.TokenError, IndentationError, SyntaxError) as exc:
            parse_errors.append(
                f"[COMMENT_RATCHET] {rel_posix}:1: Failed to tokenize {lang} file ({exc}). Fix: Fix syntax error before running comment ratchet."
            )
            continue
        kept = [c for c in raw_comments if not is_exempt_comment(lang, c)]
        if kept:
            ordinary_by_file[rel_posix] = kept

    total = sum(len(v) for v in ordinary_by_file.values())
    errors = list(parse_errors)
    for rel_posix, comments in sorted(ordinary_by_file.items()):
        file_limit = file_limits.get(rel_posix)
        exceeds_file = file_limit is not None and len(comments) > file_limit
        if total > max_total or exceeds_file:
            limit_desc = (
                f"file limit={file_limit} (found {len(comments)})"
                if exceeds_file
                else f"max_total={max_total} (found {total})"
            )
            for comment in comments:
                snippet = comment.text.splitlines()[0].strip()[:80]
                errors.append(
                    f"[COMMENT_RATCHET] {rel_posix}:{comment.start_line}: "
                    f"Ordinary comment exceeds {limit_desc}: {snippet!r}. Fix: {fix}"
                )
    return errors, total, max_total
