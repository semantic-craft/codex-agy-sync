#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
from pathlib import Path
import re
import sys
import tomllib

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from _comment_lexer import discover_repo_files, mask_swift_source, scan_comment_ratchet  # noqa: E402

RAW_SQLITE_ALLOWLIST: frozenset[str] = frozenset({
    "bridge/paths_and_locks.py",
})

TMP_GUARD_ALLOWLIST: frozenset[str] = frozenset({
    "skills/verify-codex-agy-sync/scripts/control_sync_verify.py",
    "skills/verify-codex-agy-sync/scripts/_workbench_sandbox.py",
})
TMP_GUARD_FUNC_NAMES: frozenset[str] = frozenset({"is_tmp_path", "_is_tmp_path"})
TMP_GUARD_EXACT_ROOTS: frozenset[str] = frozenset({"/tmp", "/private/tmp"})

TMP_PATH_CALL_TARGETS: frozenset[str] = frozenset({
    "Path",
    "PurePath",
    "PosixPath",
    "pathlib.Path",
    "pathlib.PurePath",
    "pathlib.PosixPath",
    "os.path.join",
    "os.makedirs",
    "os.mkdir",
    "shutil.rmtree",
    "sqlite3.connect",
    "open",
    "TemporaryDirectory",
    "NamedTemporaryFile",
    "mkdtemp",
    "mkstemp",
    "tempfile.TemporaryDirectory",
    "tempfile.NamedTemporaryFile",
    "tempfile.mkdtemp",
    "tempfile.mkstemp",
})

SWIFT_CLICLIENT_PATH = "macos/Sources/CodexAgySyncCore/CLIClient.swift"
SWIFT_DEFAULTS_ALLOWLIST: frozenset[str] = frozenset({
    "macos/Sources/CodexAgySyncCore/PreferencesStore.swift",
    "macos/Sources/CodexAgySyncCore/UpdatePathGuard.swift",
})

SUBPROCESS_CALLABLES: frozenset[str] = frozenset({
    "Popen",
    "call",
    "check_call",
    "check_output",
    "run",
})

SWIFT_RAW_KILL_RE = re.compile(r"(?:(?<![A-Za-z0-9_.])|\b(?:Darwin|Glibc|Foundation)\.)kill\s*\(")
SWIFT_TERMINATE_FN_RE = re.compile(r"\bfunc\s+terminateProcessWithEscalation\s*\([^)]*\)[^{]*\{")
SWIFT_PID_GUARD_RE = re.compile(r"\b(?:if|guard)\b[^{;\n]*\bpid\s*>\s*0\b")
SWIFT_USERDEFAULTS_SUITE_RE = re.compile(r"\bUserDefaults\s*\(\s*suiteName\s*:")


@dataclass(frozen=True)
class InvariantViolation:
    rule: str
    path: str
    line: int
    detail: str
    fix: str

    def format(self) -> str:
        return f"[{self.rule}] {self.path}:{self.line}: {self.detail} Fix: {self.fix}"


def _parse_python_file(path: Path, rel_repo: str) -> tuple[ast.Module | None, InvariantViolation | None]:
    try:
        src = path.read_text(encoding="utf-8")
        return ast.parse(src, filename=str(path)), None
    except UnicodeDecodeError as exc:
        return None, InvariantViolation(
            rule="PARSE_ERROR",
            path=rel_repo,
            line=1,
            detail=f"Invalid UTF-8 encoding ({exc}).",
            fix="Save the Python source file as valid UTF-8 so AST invariants can verify it.",
        )
    except SyntaxError as exc:
        return None, InvariantViolation(
            rule="PARSE_ERROR",
            path=rel_repo,
            line=exc.lineno or 1,
            detail=f"Python syntax error ({exc.msg}).",
            fix="Fix the syntax error so AST invariant checks can inspect this file.",
        )
    except OSError as exc:
        return None, InvariantViolation(
            rule="PARSE_ERROR",
            path=rel_repo,
            line=1,
            detail=f"Unable to read Python file ({exc}).",
            fix="Ensure the Python source file is readable.",
        )


def _load_python_trees(
    repo_root: Path,
    roots: tuple[str, ...],
) -> tuple[list[tuple[str, ast.Module, dict[str, str], dict[str, str]]], list[InvariantViolation]]:
    parsed: list[tuple[str, ast.Module, dict[str, str], dict[str, str]]] = []
    errors: list[InvariantViolation] = []
    for rel in discover_repo_files(repo_root, roots=roots, suffix=".py"):
        rel_posix = rel.as_posix()
        tree, err = _parse_python_file(repo_root / rel, rel_posix)
        if err is not None:
            errors.append(err)
            continue
        assert tree is not None
        mod_aliases, sym_aliases = _collect_imports(tree)
        parsed.append((rel_posix, tree, mod_aliases, sym_aliases))
    return parsed, errors


def _dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        return f"{parent}.{node.attr}" if parent else None
    return None


def _collect_imports(tree: ast.Module) -> tuple[dict[str, str], dict[str, str]]:
    mod_aliases: dict[str, str] = {}
    sym_aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                target = alias.name if alias.asname else alias.name.split(".")[0]
                mod_aliases[bound] = target
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for alias in node.names:
                if alias.name == "*":
                    continue
                bound = alias.asname or alias.name
                sym_aliases[bound] = f"{node.module}.{alias.name}"
    return mod_aliases, sym_aliases


def _resolve_call_target(func: ast.AST, mod_aliases: dict[str, str], sym_aliases: dict[str, str]) -> str | None:
    dotted = _dotted_name(func)
    if not dotted:
        return None
    parts = dotted.split(".")
    head = parts[0]
    if head in sym_aliases:
        return ".".join([sym_aliases[head], *parts[1:]])
    if head in mod_aliases:
        return ".".join([mod_aliases[head], *parts[1:]])
    return dotted


def _check_raw_sqlite_connect(
    parsed_files: list[tuple[str, ast.Module, dict[str, str], dict[str, str]]],
) -> list[InvariantViolation]:
    fix = "Use open_readonly_sqlite() for reads or open_locked_rw_sqlite(path, permit) for locked/staging writes."
    prefix = "src/codex_agy_sync/"
    violations: list[InvariantViolation] = []
    for rel_repo, tree, mod_aliases, sym_aliases in parsed_files:
        if not rel_repo.startswith(prefix):
            continue
        rel_pkg = rel_repo.removeprefix(prefix)
        if rel_pkg in RAW_SQLITE_ALLOWLIST:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if _resolve_call_target(node.func, mod_aliases, sym_aliases) == "sqlite3.connect":
                violations.append(
                    InvariantViolation(
                        rule="RAW_SQLITE_CONNECT",
                        path=rel_repo,
                        line=node.lineno,
                        detail="Disallowed direct sqlite3.connect() call outside allowlisted bridge modules.",
                        fix=fix,
                    )
                )
    return violations


def _check_bare_urlopen(
    parsed_files: list[tuple[str, ast.Module, dict[str, str], dict[str, str]]],
) -> list[InvariantViolation]:
    fix = "Use urllib.request.build_opener(urllib.request.ProxyHandler({})).open(...) to bypass system proxies on loopback RPCs."
    violations: list[InvariantViolation] = []
    for rel_repo, tree, mod_aliases, sym_aliases in parsed_files:
        if not rel_repo.startswith(("src/", "scripts/", "skills/")):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if _resolve_call_target(node.func, mod_aliases, sym_aliases) == "urllib.request.urlopen":
                violations.append(
                    InvariantViolation(
                        rule="BARE_URLOPEN",
                        path=rel_repo,
                        line=node.lineno,
                        detail="Bare urllib.request.urlopen() inherits system HTTP/HTTPS proxy settings.",
                        fix=fix,
                    )
                )
    return violations


def _is_tmp_str(val: str) -> bool:
    return val == "/tmp" or val.startswith("/tmp/") or val == "/private/tmp" or val.startswith("/private/tmp/")


def _extract_tmp_literal(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value if _is_tmp_str(node.value) else None
    if isinstance(node, ast.JoinedStr) and node.values:
        first = node.values[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str) and _is_tmp_str(first.value):
            return first.value
    return None


def _is_path_root_call(node: ast.AST, mod_aliases: dict[str, str], sym_aliases: dict[str, str]) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    target = _resolve_call_target(node.func, mod_aliases, sym_aliases)
    if target not in ("Path", "PurePath", "PosixPath", "pathlib.Path", "pathlib.PurePath", "pathlib.PosixPath"):
        return None
    if len(node.args) == 1 and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
        return node.args[0].value
    return None


def _walk_tmp_checks(
    node: ast.AST,
    rel_repo: str,
    mod_aliases: dict[str, str],
    sym_aliases: dict[str, str],
    in_guard_fn: bool,
    out: list[InvariantViolation],
) -> None:
    fix = "Use $HOME/Projects/_artifacts/codex-agy-sync/ or repo out/ instead of /tmp."
    next_guard_fn = in_guard_fn
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        if rel_repo in TMP_GUARD_ALLOWLIST and node.name in TMP_GUARD_FUNC_NAMES:
            next_guard_fn = True

    if isinstance(node, ast.Call):
        target = _resolve_call_target(node.func, mod_aliases, sym_aliases)
        if target == "tempfile.gettempdir":
            out.append(
                InvariantViolation(
                    rule="HARDCODED_TMP_PATH",
                    path=rel_repo,
                    line=node.lineno,
                    detail="Disallowed tempfile.gettempdir() call.",
                    fix=fix,
                )
            )
        elif target in TMP_PATH_CALL_TARGETS:
            arg_nodes = [*node.args, *(kw.value for kw in node.keywords)]
            for arg in arg_nodes:
                lit = _extract_tmp_literal(arg)
                if lit is None:
                    continue
                if next_guard_fn and lit in TMP_GUARD_EXACT_ROOTS:
                    continue
                out.append(
                    InvariantViolation(
                        rule="HARDCODED_TMP_PATH",
                        path=rel_repo,
                        line=node.lineno,
                        detail=f"Hardcoded /tmp path construction via {target}({lit!r}).",
                        fix=fix,
                    )
                )
                break
    elif isinstance(node, ast.BinOp):
        if isinstance(node.op, ast.Div):
            left_root = _is_path_root_call(node.left, mod_aliases, sym_aliases)
            if left_root in ("/", "/private") and isinstance(node.right, ast.Constant) and isinstance(node.right.value, str):
                seg = node.right.value.lstrip("/")
                if seg == "tmp" or seg.startswith("tmp/"):
                    out.append(
                        InvariantViolation(
                            rule="HARDCODED_TMP_PATH",
                            path=rel_repo,
                            line=node.lineno,
                            detail=f"Hardcoded /tmp path construction via Path({left_root!r}) / {node.right.value!r}.",
                            fix=fix,
                        )
                    )
        elif isinstance(node.op, ast.Add):
            lit = _extract_tmp_literal(node.left)
            if lit is not None:
                out.append(
                    InvariantViolation(
                        rule="HARDCODED_TMP_PATH",
                        path=rel_repo,
                        line=node.lineno,
                        detail=f"Hardcoded /tmp path string concatenation ({lit!r} + ...).",
                        fix=fix,
                    )
                )

    for child in ast.iter_child_nodes(node):
        _walk_tmp_checks(child, rel_repo, mod_aliases, sym_aliases, next_guard_fn, out)


def _check_hardcoded_tmp_path(
    parsed_files: list[tuple[str, ast.Module, dict[str, str], dict[str, str]]],
) -> list[InvariantViolation]:
    violations: list[InvariantViolation] = []
    for rel_repo, tree, mod_aliases, sym_aliases in parsed_files:
        if not rel_repo.startswith(("src/", "scripts/", "skills/")):
            continue
        _walk_tmp_checks(tree, rel_repo, mod_aliases, sym_aliases, False, violations)
    return violations


def _find_line_number(text: str, needle: str) -> int:
    for line_no, line in enumerate(text.splitlines(), start=1):
        if needle in line:
            return line_no
    return 1


def _parse_toml_version(path: Path, keys: tuple[tuple[str, ...], ...]) -> tuple[str | None, int, str | None]:
    try:
        text = path.read_text(encoding="utf-8")
        data = tomllib.loads(text)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return None, 1, str(exc)
    for key_path in keys:
        cur: object = data
        for seg in key_path:
            if isinstance(cur, dict) and seg in cur:
                cur = cur[seg]
            else:
                cur = None
                break
        if isinstance(cur, str) and cur.strip():
            return cur, _find_line_number(text, f'"{cur}"'), None
    return None, 1, None


def _parse_regex_version(path: Path, pattern: re.Pattern[str]) -> tuple[str | None, int, str | None]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, 1, str(exc)
    m = pattern.search(text)
    if not m:
        return None, 1, None
    line_no = text.count("\n", 0, m.start()) + 1
    return m.group(1), line_no, None


def _parse_swift_fallback_version(path: Path) -> tuple[str | None, int, str | None]:
    try:
        code_only = mask_swift_source(path.read_text(encoding="utf-8"), mask_strings=False)
    except OSError as exc:
        return None, 1, str(exc)
    lines = code_only.splitlines()
    for idx, line in enumerate(lines):
        if "CFBundleShortVersionString" in line:
            for offset in range(0, 6):
                if idx + offset >= len(lines):
                    break
                m = re.search(r'\?\?\s*"([^"]+)"', lines[idx + offset])
                if m:
                    return m.group(1), idx + offset + 1, None
    return None, 1, None


def _check_version_sync(repo_root: Path) -> list[InvariantViolation]:
    pyproject_path = repo_root / "pyproject.toml"
    if not pyproject_path.is_file():
        return [
            InvariantViolation(
                rule="VERSION_SYNC",
                path="pyproject.toml",
                line=1,
                detail="Missing pyproject.toml canonical version file.",
                fix='Restore pyproject.toml with [project] version = "<semver>".',
            )
        ]
    expected_ver, py_line, py_err = _parse_toml_version(pyproject_path, (("project", "version"),))
    if py_err or not expected_ver:
        return [
            InvariantViolation(
                rule="VERSION_SYNC",
                path="pyproject.toml",
                line=py_line,
                detail=f"Unable to parse [project].version in pyproject.toml ({py_err or 'missing key'}).",
                fix='Define version = "<semver>" under [project] in pyproject.toml.',
            )
        ]

    specs = (
        (
            "src/codex_agy_sync/workspace.py",
            lambda p: _parse_regex_version(
                p, re.compile(r'^PACKAGE_VERSION(?:\s*:\s*str)?\s*=\s*"([^"]+)"', re.M)
            ),
        ),
        (
            "rust/Cargo.toml",
            lambda p: _parse_toml_version(p, (("workspace", "package", "version"), ("package", "version"))),
        ),
        (
            "rust/crates/cas-core/src/id.rs",
            lambda p: _parse_regex_version(
                p, re.compile(r'^pub\s+const\s+PACKAGE_VERSION\s*:\s*&str\s*=\s*"([^"]+)";', re.M)
            ),
        ),
        (
            "macos/Sources/CodexAgySyncCore/UpdateService.swift",
            _parse_swift_fallback_version,
        ),
    )

    violations: list[InvariantViolation] = []
    for rel_path, parser_fn in specs:
        full = repo_root / rel_path
        if not full.is_file():
            violations.append(
                InvariantViolation(
                    rule="VERSION_SYNC",
                    path=rel_path,
                    line=1,
                    detail=f"Missing versioned source file (expected {expected_ver!r} from pyproject.toml).",
                    fix=f"Restore {rel_path} and set its version to {expected_ver!r}.",
                )
            )
            continue
        found_ver, line_no, err = parser_fn(full)
        if err or found_ver != expected_ver:
            detail = (
                f"Failed to parse {rel_path} ({err})."
                if err
                else f"Version {found_ver!r} in {rel_path} does not match pyproject.toml version {expected_ver!r}."
            )
            violations.append(
                InvariantViolation(
                    rule="VERSION_SYNC",
                    path=rel_path,
                    line=line_no,
                    detail=detail,
                    fix=f"Update {rel_path} to version {expected_ver!r} so Python, Rust, and Swift versions match.",
                )
            )
    return violations


def _find_balanced_span(text: str, open_idx: int, open_ch: str, close_ch: str) -> int:
    depth = 0
    for idx in range(open_idx, len(text)):
        if text[idx] == open_ch:
            depth += 1
        elif text[idx] == close_ch:
            depth -= 1
            if depth == 0:
                return idx + 1
    return len(text)


def _check_swift_raw_kill(repo_root: Path) -> list[InvariantViolation]:
    fix = "Route process termination through CLIClient terminateProcessWithEscalation() with a pid > 0 guard to avoid kill(0, SIGKILL)."
    violations: list[InvariantViolation] = []
    for rel in discover_repo_files(repo_root, roots=("macos/Sources",), suffix=".swift"):
        rel_repo = rel.as_posix()
        masked = mask_swift_source((repo_root / rel).read_text(encoding="utf-8"), mask_strings=True)
        matches = list(SWIFT_RAW_KILL_RE.finditer(masked))
        if not matches:
            continue
        if rel_repo != SWIFT_CLICLIENT_PATH:
            for match in matches:
                line_no = masked.count("\n", 0, match.start()) + 1
                violations.append(
                    InvariantViolation(
                        rule="SWIFT_RAW_KILL",
                        path=rel_repo,
                        line=line_no,
                        detail="Direct POSIX kill() call outside CLIClient.swift.",
                        fix=fix,
                    )
                )
            continue

        allowed_spans: list[tuple[int, int, bool]] = []
        for fn_match in SWIFT_TERMINATE_FN_RE.finditer(masked):
            brace_idx = fn_match.end() - 1
            end_idx = _find_balanced_span(masked, brace_idx, "{", "}")
            body = masked[brace_idx:end_idx]
            allowed_spans.append((brace_idx, end_idx, bool(SWIFT_PID_GUARD_RE.search(body))))
        for match in matches:
            line_no = masked.count("\n", 0, match.start()) + 1
            enclosing = next((sp for sp in allowed_spans if sp[0] <= match.start() < sp[1]), None)
            if enclosing is None:
                violations.append(
                    InvariantViolation(
                        rule="SWIFT_RAW_KILL",
                        path=rel_repo,
                        line=line_no,
                        detail="Direct POSIX kill() call in CLIClient.swift outside terminateProcessWithEscalation().",
                        fix=fix,
                    )
                )
            elif not enclosing[2]:
                violations.append(
                    InvariantViolation(
                        rule="SWIFT_RAW_KILL",
                        path=rel_repo,
                        line=line_no,
                        detail="kill() inside terminateProcessWithEscalation() is missing a pid > 0 guard.",
                        fix=fix,
                    )
                )
    return violations


def _check_swift_reverse_dns_defaults(repo_root: Path) -> list[InvariantViolation]:
    fix = "Inject a sandbox file-path UserDefaults suite in tests/QA or route production preferences through PreferencesStore."
    violations: list[InvariantViolation] = []
    for rel in discover_repo_files(repo_root, roots=("macos/Sources",), suffix=".swift"):
        rel_repo = rel.as_posix()
        if rel_repo in SWIFT_DEFAULTS_ALLOWLIST:
            continue
        code_only = mask_swift_source((repo_root / rel).read_text(encoding="utf-8"), mask_strings=False)
        for match in SWIFT_USERDEFAULTS_SUITE_RE.finditer(code_only):
            open_paren = code_only.find("(", match.start())
            if open_paren == -1:
                continue
            call_end = _find_balanced_span(code_only, open_paren, "(", ")")
            call_slice = code_only[open_paren:call_end]
            if "dev.semanticcraft." in call_slice:
                line_no = code_only.count("\n", 0, match.start()) + 1
                violations.append(
                    InvariantViolation(
                        rule="SWIFT_REVERSE_DNS_DEFAULTS",
                        path=rel_repo,
                        line=line_no,
                        detail="Reverse-DNS UserDefaults(suiteName:) outside PreferencesStore.swift/UpdatePathGuard.swift.",
                        fix=fix,
                    )
                )
    return violations


def _is_empty_collection_expr(node: ast.AST) -> bool:
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return len(node.elts) == 0
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("tuple", "list", "set"):
        return len(node.args) == 0 and len(node.keywords) == 0
    return False


def _check_subprocess_close_fds_false(
    parsed_files: list[tuple[str, ast.Module, dict[str, str], dict[str, str]]],
) -> list[InvariantViolation]:
    fix = "Use close_fds=True without pass_fds so child processes do not inherit flock(2) descriptors on macOS/BSD."
    violations: list[InvariantViolation] = []
    for rel_repo, tree, mod_aliases, sym_aliases in parsed_files:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = _resolve_call_target(node.func, mod_aliases, sym_aliases)
            if not target or not target.startswith("subprocess."):
                continue
            fn_name = target.removeprefix("subprocess.")
            if fn_name not in SUBPROCESS_CALLABLES:
                continue
            for kw in node.keywords:
                if kw.arg == "close_fds" and isinstance(kw.value, ast.Constant):
                    if isinstance(kw.value.value, (bool, int)) and kw.value.value == 0:
                        violations.append(
                            InvariantViolation(
                                rule="SUBPROCESS_CLOSE_FDS_FALSE",
                                path=rel_repo,
                                line=node.lineno,
                                detail=f"subprocess.{fn_name}(..., close_fds={kw.value.value!r}) leaks file locks to child processes.",
                                fix=fix,
                            )
                        )
                elif kw.arg == "pass_fds" and not _is_empty_collection_expr(kw.value):
                    violations.append(
                        InvariantViolation(
                            rule="SUBPROCESS_CLOSE_FDS_FALSE",
                            path=rel_repo,
                            line=node.lineno,
                            detail=f"subprocess.{fn_name}(..., pass_fds=...) passes open file descriptors to child processes.",
                            fix=fix,
                        )
                    )
    return violations


def check_comment_ratchet(repo_root: Path) -> list[str]:
    errors, _, _ = scan_comment_ratchet(repo_root)
    return errors


def check_repo_invariants(repo_root: Path) -> list[str]:
    parsed_files, parse_errors = _load_python_trees(repo_root, ("src", "scripts", "skills", "tests"))
    violations: list[InvariantViolation] = [
        *parse_errors,
        *_check_raw_sqlite_connect(parsed_files),
        *_check_bare_urlopen(parsed_files),
        *_check_hardcoded_tmp_path(parsed_files),
        *_check_version_sync(repo_root),
        *_check_swift_raw_kill(repo_root),
        *_check_swift_reverse_dns_defaults(repo_root),
        *_check_subprocess_close_fds_false(parsed_files),
    ]
    return [v.format() for v in violations]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Self-contained repository invariant lints and comment ratchet.")
    parser.add_argument("--root", default="", help="Repository root directory (defaults to parent of scripts/).")
    parser.add_argument("--check-comments", action="store_true", help="Run the .comment-ratchet.json comment check.")
    args = parser.parse_args(argv)
    repo_root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parent.parent

    if args.check_comments:
        errors, total, max_total = scan_comment_ratchet(repo_root)
        if errors:
            for err in errors:
                print(err, file=sys.stderr)
            return 1
        print(f"  Self-contained comment ratchet verified ({total} ordinary comments, max_total={max_total}).")
        return 0

    errors = check_repo_invariants(repo_root)
    if errors:
        for err in errors:
            print(err, file=sys.stderr)
        return 1
    print("  Repository invariant lints verified (7 rules, 0 violations).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
