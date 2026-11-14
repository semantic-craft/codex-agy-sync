"""AST architectural coherence gate for `codex_agy_sync`."""

from __future__ import annotations

import ast
from pathlib import Path
import sys

TIER_MAP = {
    "_wire": 0,
    "workspace": 0,
    "payloads": 0,
    "events": 1,
    "ledger": 1,
    "codecs": 2,
    "bridge": 3,
    "workbench": 4,
}


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def tier_for_module(mod: str, pkg_name: str = "codex_agy_sync") -> int:
    sub = mod.removeprefix(pkg_name)
    if sub == "" or sub in (".cli", ".__main__"):
        return 5
    top = sub.lstrip(".").split(".")[0]
    if top in TIER_MAP:
        return TIER_MAP[top]
    raise AssertionError(f"Unassigned module tier for {mod}")


def _module_info(path: Path, pkg_root: Path, pkg_name: str) -> tuple[str, str]:
    parts = list(path.relative_to(pkg_root).with_suffix("").parts)
    if parts[-1] == "__init__":
        mod = ".".join([pkg_name, *parts[:-1]])
        return mod, mod
    mod = ".".join([pkg_name, *parts])
    return mod, ".".join([pkg_name, *parts[:-1]])


def _check_all_exports(tree: ast.Module, rel: str) -> None:
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets, val = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, val = [node.target], node.value
        elif isinstance(node, ast.AugAssign):
            targets, val = [node.target], node.value
        else:
            continue
        if any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets) and isinstance(val, (ast.List, ast.Tuple)):
            for elt in val.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    if elt.value.startswith("_") and elt.value != "__version__":
                        raise AssertionError(f"{rel}:{node.lineno}: private symbol {elt.value!r} in __all__")


def _check_function_internal_imports(tree: ast.Module, rel: str, pkg_name: str) -> None:
    prefix = f"{pkg_name}."
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sub in ast.walk(fn):
            if isinstance(sub, ast.Import):
                for al in sub.names:
                    if al.name == pkg_name or al.name.startswith(prefix):
                        raise AssertionError(f"{rel}:{sub.lineno}: function-scoped internal import {al.name}")
            elif isinstance(sub, ast.ImportFrom):
                mod = sub.module or ""
                if sub.level > 0 or mod == pkg_name or mod.startswith(prefix):
                    raise AssertionError(f"{rel}:{sub.lineno}: function-scoped internal import from {mod or '.'}")


def _check_private_attr_access(tree: ast.Module, rel: str) -> None:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        if node.attr.startswith("_") and not _is_dunder(node.attr):
            if isinstance(node.value, ast.Name) and node.value.id not in ("self", "cls"):
                raise AssertionError(f"{rel}:{node.lineno}: non-self/cls private attribute access {node.value.id}.{node.attr}")


def _validate_module_segments(dotted: str, rel: str, lineno: int) -> None:
    for seg in dotted.split("."):
        if seg.startswith("_") and seg != "_wire" and not _is_dunder(seg):
            raise AssertionError(f"{rel}:{lineno}: disallowed private module segment {dotted!r}")


def _resolve_import_targets(
    node: ast.AST, cur_pkg: str, pkg_name: str, known_mods: set[str], rel: str,
) -> set[str]:
    prefix = f"{pkg_name}."
    targets: set[str] = set()
    if isinstance(node, ast.Import):
        for al in node.names:
            _validate_module_segments(al.name, rel, node.lineno)
            if al.name == pkg_name or al.name.startswith(prefix):
                if al.name not in known_mods:
                    raise AssertionError(f"{rel}:{node.lineno}: unresolved internal import {al.name}")
                targets.add(al.name)
    elif isinstance(node, ast.ImportFrom):
        if node.module:
            _validate_module_segments(node.module, rel, node.lineno)
        for al in node.names:
            if al.name.startswith("_") and al.name != "__version__":
                raise AssertionError(f"{rel}:{node.lineno}: private cross-module import {al.name!r}")
        if node.level == 0:
            if not node.module or (node.module != pkg_name and not node.module.startswith(prefix)):
                return targets
            base = node.module
        else:
            pkg_parts = cur_pkg.split(".")
            up = node.level - 1
            if up >= len(pkg_parts):
                raise AssertionError(f"{rel}:{node.lineno}: relative import level={node.level} escapes {pkg_name}")
            base_parts = pkg_parts[: len(pkg_parts) - up]
            base = ".".join([*base_parts, *node.module.split(".")]) if node.module else ".".join(base_parts)
        for al in node.names:
            cand = f"{base}.{al.name}"
            if node.module is None and cand in known_mods:
                targets.add(cand)
            elif base in known_mods:
                targets.add(base)
            elif cand in known_mods:
                targets.add(cand)
            else:
                raise AssertionError(f"{rel}:{node.lineno}: unresolved internal import {base}.{al.name}")
    return targets


def _find_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    state: dict[str, int] = {}
    stack: list[str] = []
    cycles: list[list[str]] = []

    def dfs(node: str) -> None:
        state[node] = 1
        stack.append(node)
        for nxt in sorted(graph.get(node, ())):
            st = state.get(nxt, 0)
            if st == 1:
                cycles.append([*stack[stack.index(nxt) :], nxt])
            elif st == 0:
                dfs(nxt)
        stack.pop()
        state[node] = 2

    for mod in sorted(graph):
        if state.get(mod, 0) == 0:
            dfs(mod)
    return cycles


def check_package_architecture(pkg_root: Path, pkg_name: str = "codex_agy_sync") -> dict[str, int]:
    """Enforce zero private leaks, strict 6-tier downward DAG, and zero import cycles."""
    files = sorted(pkg_root.rglob("*.py"))
    file_to_mod = {p: _module_info(p, pkg_root, pkg_name) for p in files}
    known_mods = {mod for mod, _ in file_to_mod.values()}
    graph: dict[str, set[str]] = {mod: set() for mod in known_mods}

    for path, (cur_mod, cur_pkg) in file_to_mod.items():
        rel = str(path.relative_to(pkg_root))
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        _check_all_exports(tree, rel)
        _check_function_internal_imports(tree, rel, pkg_name)
        _check_private_attr_access(tree, rel)
        src_tier = tier_for_module(cur_mod, pkg_name)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for tgt in _resolve_import_targets(node, cur_pkg, pkg_name, known_mods, rel):
                    graph[cur_mod].add(tgt)
                    tgt_tier = tier_for_module(tgt, pkg_name)
                    if src_tier < tgt_tier:
                        raise AssertionError(f"{rel}: upward tier dependency {cur_mod} (T{src_tier}) -> {tgt} (T{tgt_tier})")

    cycles = _find_cycles(graph)
    if cycles:
        formatted = " -> ".join(cycles[0])
        raise AssertionError(f"Internal import cycle detected ({len(cycles)}): {formatted}")
    for path, (cur_mod, _) in file_to_mod.items():
        _validate_module_segments(cur_mod, str(path.relative_to(pkg_root)), 1)
    return {"modules": len(file_to_mod), "edges": sum(len(v) for v in graph.values())}


def main() -> None:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "src" / "codex_agy_sync"
    summary = check_package_architecture(root)
    print(f"  AST architectural coherence verified: {summary['modules']} modules, {summary['edges']} edges, 0 cycles, strict 6-tier DAG.")


if __name__ == "__main__":
    main()
