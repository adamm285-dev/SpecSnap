"""AST Module-Boundary Checker for SpecSnap (`wiring.yaml`).

Audits all Python files inside package directories against the allowed subsystem
dependencies declared in `wiring.yaml`. Unlisted helper modules, syntax errors,
and dynamic import bypasses (`importlib`, `__import__`, `sys.modules`) are
flagged as boundary violations.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SubsystemSpec:
    name: str
    rel_path: str
    allowed_dependencies: frozenset[str]


def _parse_wiring_yaml(wiring_path: Path) -> dict[str, SubsystemSpec]:
    """Parse the `subsystems:` mapping in `wiring.yaml` without external dependencies."""
    text = wiring_path.read_text(encoding="utf-8").replace("\r\n", "\n")
    try:
        import yaml  # type: ignore[import-untyped]

        raw = yaml.safe_load(text) or {}
        subs_raw = raw.get("subsystems", {}) or {}
        specs: dict[str, SubsystemSpec] = {}
        for name, cfg in subs_raw.items():
            p_str = str(cfg.get("path", "")).strip().strip("\"'").replace("\\", "/")
            deps = frozenset(str(d).strip().strip("\"'") for d in (cfg.get("allowed_dependencies") or []))
            specs[str(name)] = SubsystemSpec(
                name=str(name),
                rel_path=p_str,
                allowed_dependencies=deps,
            )
        return specs
    except Exception:
        specs = {}
        in_subsystems = False
        cur_name: str | None = None
        cur_path: str = ""
        cur_deps: list[str] = []
        in_deps_list = False

        def _flush() -> None:
            nonlocal cur_name, cur_path, cur_deps
            if cur_name and cur_path:
                specs[cur_name] = SubsystemSpec(
                    name=cur_name,
                    rel_path=cur_path,
                    allowed_dependencies=frozenset(cur_deps),
                )
            cur_name = None
            cur_path = ""
            cur_deps = []

        for raw_line in text.splitlines():
            line = raw_line.split("#", 1)[0].rstrip()
            if not line.strip():
                continue
            indent = len(line) - len(line.lstrip(" "))
            stripped = line.strip()
            if indent == 0:
                if stripped.startswith("subsystems:"):
                    in_subsystems = True
                else:
                    _flush()
                    in_subsystems = False
                continue
            if not in_subsystems:
                continue
            if indent == 2 and stripped.endswith(":"):
                _flush()
                cur_name = stripped[:-1].strip().strip("\"'")
                in_deps_list = False
            elif indent == 4 and cur_name:
                if stripped.startswith("path:"):
                    cur_path = stripped.split(":", 1)[1].strip().strip("\"'").replace("\\", "/")
                    in_deps_list = False
                elif stripped.startswith("allowed_dependencies:"):
                    rest = stripped.split(":", 1)[1].strip()
                    if rest.startswith("[") and rest.endswith("]"):
                        inner = rest[1:-1].strip()
                        if inner:
                            cur_deps.extend(
                                item.strip().strip("\"'")
                                for item in inner.split(",")
                                if item.strip()
                            )
                        in_deps_list = False
                    else:
                        in_deps_list = True
                elif in_deps_list and stripped.startswith("-"):
                    dep_val = stripped[1:].strip().strip("\"'")
                    if dep_val:
                        cur_deps.append(dep_val)
            elif indent > 4 and in_deps_list and stripped.startswith("-"):
                dep_val = stripped[1:].strip().strip("\"'")
                if dep_val:
                    cur_deps.append(dep_val)
        _flush()
        return specs


def _build_module_lookup(
    specs: dict[str, SubsystemSpec],
) -> tuple[dict[str, str], dict[str, str], set[str]]:
    """Return `(rel_path_to_sub, module_dotted_to_sub, package_roots)`."""
    rel_to_sub: dict[str, str] = {}
    mod_to_sub: dict[str, str] = {}
    pkg_roots: set[str] = set()
    bare_counts: dict[str, int] = {}

    for sub_name, spec in specs.items():
        rel_norm = spec.rel_path.replace("\\", "/").lstrip("/")
        rel_to_sub[rel_norm] = sub_name
        if "/" in rel_norm:
            pkg_roots.add(rel_norm.split("/", 1)[0])
        if rel_norm.endswith(".py"):
            dotted = rel_norm[:-3].replace("/", ".")
            mod_to_sub[dotted] = sub_name
            parts = dotted.split(".")
            if len(parts) == 2:
                bare_counts[parts[-1]] = bare_counts.get(parts[-1], 0) + 1

    # Only map bare module names when unambiguous (e.g. single-package tasks)
    if len(pkg_roots) == 1:
        for sub_name, spec in specs.items():
            rel_norm = spec.rel_path.replace("\\", "/").lstrip("/")
            if rel_norm.endswith(".py"):
                parts = rel_norm[:-3].split("/")
                if len(parts) == 2 and bare_counts.get(parts[-1], 0) == 1:
                    mod_to_sub.setdefault(parts[-1], sub_name)

    return rel_to_sub, mod_to_sub, pkg_roots


def check_boundaries(work_dir: Path, wiring_path: Path) -> list[dict[str, Any]]:
    """
    Audit Python files in `work_dir` against the subsystem rules in `wiring_path`.
    Scans all `*.py` files under every managed package root (deny-by-default for
    unlisted helper modules, syntax errors, and dynamic import bypasses).
    """
    specs = _parse_wiring_yaml(wiring_path.resolve())
    rel_to_sub, mod_to_sub, pkg_roots = _build_module_lookup(specs)
    work_root = work_dir.resolve()
    results: list[dict[str, Any]] = []

    # Discover every *.py file inside the workspace (excluding tests/ and hidden/cache dirs)
    files_to_scan: set[str] = set(rel_to_sub.keys())
    for py_p in sorted(work_root.rglob("*.py")):
        rel_parts = py_p.relative_to(work_root).parts
        if any(
            part.startswith(".") or part in ("__pycache__", "tests", "venv", ".venv")
            for part in rel_parts
        ):
            continue
        files_to_scan.add(py_p.relative_to(work_root).as_posix())

    FORBIDDEN_TOP_MODULES = frozenset({"_pytest", "pytest", "pluggy", "importlib", "ctypes", "builtins"})
    FORBIDDEN_STR_LITERALS = frozenset({"__import__", "import_module", "_pytest", "pluggy", "sys.modules"})

    for rel_path in sorted(files_to_scan):
        py_file = work_root / rel_path
        if not py_file.is_file():
            continue

        is_init = rel_path.endswith("/__init__.py") or rel_path == "__init__.py"
        in_managed_pkg = "/" in rel_path and rel_path.split("/", 1)[0] in pkg_roots
        source_sub = rel_to_sub.get(rel_path)

        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8", errors="replace"), filename=rel_path)
        except SyntaxError as exc:
            results.append(
                {
                    "source_file": rel_path,
                    "line_number": exc.lineno or 1,
                    "source_subsystem": source_sub or rel_path,
                    "target_subsystem": "<syntax_error>",
                    "imported_module": "SyntaxError",
                    "allowed_dependencies": sorted(specs[source_sub].allowed_dependencies) if source_sub else [],
                }
            )
            continue

        if source_sub is None and in_managed_pkg and not is_init:
            results.append(
                {
                    "source_file": rel_path,
                    "line_number": 1,
                    "source_subsystem": "<unlisted_module>",
                    "target_subsystem": "<wiring_violation>",
                    "imported_module": rel_path,
                    "allowed_dependencies": [],
                }
            )
            continue

        pkg_prefix = rel_path.split("/")[0] if "/" in rel_path else ""
        # An unlisted `__init__.py` has `allowed = frozenset()`, so any subsystem import inside
        # `__init__.py` is checked and rejected instead of acting as a re-export bridge.
        allowed = specs[source_sub].allowed_dependencies if source_sub else frozenset()
        sub_label = source_sub or rel_path

        for node in ast.walk(tree):
            # Flag dynamic import / test-runner tampering bypasses
            if isinstance(node, ast.Call):
                func = node.func
                lineno = getattr(node, "lineno", 1)
                if isinstance(func, ast.Name) and func.id in ("__import__", "exec", "eval", "compile"):
                    results.append(
                        {
                            "source_file": rel_path,
                            "line_number": lineno,
                            "source_subsystem": sub_label,
                            "target_subsystem": "<dynamic_import_blocked>",
                            "imported_module": func.id,
                            "allowed_dependencies": sorted(allowed),
                        }
                    )
                elif isinstance(func, ast.Name) and func.id in ("getattr", "setattr", "delattr"):
                    for arg in node.args:
                        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                            if arg.value in FORBIDDEN_STR_LITERALS or arg.value in ("modules", "runtest", "_exit"):
                                results.append(
                                    {
                                        "source_file": rel_path,
                                        "line_number": lineno,
                                        "source_subsystem": sub_label,
                                        "target_subsystem": "<dynamic_import_blocked>",
                                        "imported_module": f"{func.id}(..., {arg.value!r})",
                                        "allowed_dependencies": sorted(allowed),
                                    }
                                )
                elif isinstance(func, ast.Attribute) and func.attr in ("import_module", "__import__", "_exit"):
                    results.append(
                        {
                            "source_file": rel_path,
                            "line_number": lineno,
                            "source_subsystem": sub_label,
                            "target_subsystem": "<dynamic_import_blocked>",
                            "imported_module": f"*.{func.attr}",
                            "allowed_dependencies": sorted(allowed),
                        }
                    )
            elif isinstance(node, ast.Attribute) and node.attr in ("modules", "runtest", "__builtins__"):
                results.append(
                    {
                        "source_file": rel_path,
                        "line_number": getattr(node, "lineno", 1),
                        "source_subsystem": sub_label,
                        "target_subsystem": "<sys_modules_or_runner_blocked>",
                        "imported_module": f"*.{node.attr}",
                        "allowed_dependencies": sorted(allowed),
                    }
                )
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if any(tok in node.value for tok in ("_pytest", "pluggy", "__import__")):
                    results.append(
                        {
                            "source_file": rel_path,
                            "line_number": getattr(node, "lineno", 1),
                            "source_subsystem": sub_label,
                            "target_subsystem": "<runner_tamper_blocked>",
                            "imported_module": node.value,
                            "allowed_dependencies": sorted(allowed),
                        }
                    )

            candidates: list[tuple[str, int]] = []
            if isinstance(node, ast.Import):
                for alias in node.names:
                    candidates.append((alias.name, getattr(node, "lineno", 1)))
                    top_mod = alias.name.split(".", 1)[0]
                    if top_mod in FORBIDDEN_TOP_MODULES:
                        results.append(
                            {
                                "source_file": rel_path,
                                "line_number": getattr(node, "lineno", 1),
                                "source_subsystem": sub_label,
                                "target_subsystem": "<dynamic_import_blocked>",
                                "imported_module": alias.name,
                                "allowed_dependencies": sorted(allowed),
                            }
                        )
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                lineno = getattr(node, "lineno", 1)
                top_mod = mod.split(".", 1)[0] if mod else ""
                if top_mod in FORBIDDEN_TOP_MODULES:
                    results.append(
                        {
                            "source_file": rel_path,
                            "line_number": lineno,
                            "source_subsystem": sub_label,
                            "target_subsystem": "<dynamic_import_blocked>",
                            "imported_module": mod,
                            "allowed_dependencies": sorted(allowed),
                        }
                    )
                if node.level and node.level > 0 and pkg_prefix:
                    base_mod = f"{pkg_prefix}.{mod}" if mod else pkg_prefix
                else:
                    base_mod = mod
                if base_mod:
                    candidates.append((base_mod, lineno))
                for alias in node.names:
                    if alias.name != "*":
                        full_cand = f"{base_mod}.{alias.name}" if base_mod else alias.name
                        candidates.append((full_cand, lineno))

            seen_on_node: set[tuple[str, str]] = set()
            for imp_mod, lineno in candidates:
                target_sub = mod_to_sub.get(imp_mod)
                if not target_sub or target_sub == source_sub:
                    continue
                if target_sub not in allowed:
                    key = (target_sub, imp_mod)
                    if key in seen_on_node:
                        continue
                    seen_on_node.add(key)
                    results.append(
                        {
                            "source_file": rel_path,
                            "line_number": lineno,
                            "source_subsystem": sub_label,
                            "target_subsystem": target_sub,
                            "imported_module": imp_mod,
                            "allowed_dependencies": sorted(allowed),
                        }
                    )

    return results


def format_plain_boundary_feedback(violations: list[dict[str, Any]]) -> str:
    """Format boundary check result as plain text."""
    if not violations:
        return "[Boundary Check] PASS: 0 module-boundary violations."

    lines = [f"[Boundary Check] FAIL: {len(violations)} module-boundary violation(s) detected:"]
    for v in violations:
        lines.append(
            f"  - {v['source_file']}:{v['line_number']}: module '{v['source_subsystem']}' "
            f"imports '{v['imported_module']}' (belongs to '{v['target_subsystem']}'). "
            f"Allowed imports for '{v['source_subsystem']}': {v['allowed_dependencies']}"
        )
    return "\n".join(lines)
