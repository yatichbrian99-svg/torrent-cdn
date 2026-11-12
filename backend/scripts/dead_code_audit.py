#!/usr/bin/env python3
"""Dead-code audit — stdlib only (no vulture/radon dependency).

Scans the backend tree and reports five classes of likely-dead code:

A. **Unreachable statements** — any statement following a terminator
   (``return`` / ``raise`` / ``break`` / ``continue``) in the same block. This is
   never a style preference: it is either a leftover or a logic bug.
B. **Private definitions never referenced** — a module-level function or class
   whose name starts with ``_`` that appears EXACTLY ONCE in the whole tree (its
   own ``def``/``class`` line).
C. **Private constants never referenced** — same exactly-once rule for
   module-level ``_FOO`` / ``FOO`` assignments that are never read.
D. **``pass``-only private functions** — a private function whose body is only
   ``pass`` (a stub that was never finished, or a hook that is no longer called).
E. **Unused public definitions** — a module-level public function/class with NO
   decorator that is referenced exactly once. Decorated definitions are excluded
   because frameworks discover those by reflection (FastAPI route handlers are
   referenced only through their ``@router.get`` decorator, so counting
   references alone would flag every route in the app).

Why exactly-once and not a call graph
-------------------------------------
Reference counting is a whole-word regex over every ``.py`` file in the scanned
tree (``app/``, ``tests/``, ``scripts/``). Counting the ENTIRE tree means a
helper used only by tests is not reported, and a match inside a string literal
counts as a reference (so ``getattr(obj, "name")`` protects ``name``). The
deliberate bias is therefore toward under-reporting: every finding should be
worth a human look, never noise.

Known limitations (by design, so the output stays trustworthy):
* Names built dynamically (``f"{prefix}_{x}"``, reflection, plugin registries)
  are invisible — a dynamically-dispatched definition is not reported because
  its bare name still appears in the dispatch table's strings.
* Tests-only modules that exist to be imported by nothing are not flagged;
  only ``app/`` is checked for orphan modules (see F).
F. **Orphan modules** — a ``.py`` under ``app/`` that no import statement
   anywhere refers to.

Exit code 0 = clean, 1 = findings, 2 = bad invocation.
"""
from __future__ import annotations

import ast
import os
import re
import sys

EXCLUDED_DIRS = {"__pycache__", ".venv", "venv", "node_modules", ".git", ".pytest_cache"}

# Definition-level findings (B/C/D/E) are reported ONLY for production code.
# Test modules exist to be DISCOVERED, not imported: pytest collects every
# ``Test*`` class and ``test_*`` function by name, so "referenced exactly once"
# is the normal, correct state for them. They still count as REFERENCES (so an
# app helper used only by tests is not reported) and are still scanned for
# unreachable statements (A), which is just as much a bug in a test.
REPORT_DIRS = {"app", "scripts"}

TERMINATORS = (ast.Return, ast.Raise, ast.Break, ast.Continue)

# Bodies are the statement lists a terminator can end.
_BODY_ATTRS = ("body", "orelse", "finalbody")


def _collect_files(root: str) -> list[str]:
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED_DIRS)
        for name in sorted(filenames):
            if name.endswith(".py"):
                found.append(os.path.join(dirpath, name))
    return found


def _count_references(name: str, texts: list[str]) -> int:
    """Whole-word occurrences of `name` across every scanned file."""
    pattern = re.compile(rf"\b{re.escape(name)}\b")
    return sum(len(pattern.findall(text)) for text in texts)


def _iter_bodies(node: ast.AST):
    for attr in _BODY_ATTRS:
        body = getattr(node, attr, None)
        if isinstance(body, list):
            yield body


def _defined_names(tree: ast.Module):
    """Yield (node, name, is_private) for module-level definitions/constants."""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield node, node.name, node.name.startswith("_")
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    private = target.id.startswith("_")
                    # SCREAMING_CASE constants count as private-ish too: an
                    # unreferenced FOO_BAR is just as dead as _foo_bar.
                    yield node, target.id, private or target.id.isupper()


def _orphan_modules(app_root: str, files: list[str], texts_by_path: dict) -> list[str]:
    """app/*.py files whose module is never imported anywhere."""
    findings: list[str] = []
    for path in files:
        if not path.startswith(app_root):
            continue
        module = os.path.splitext(os.path.basename(path))[0]
        if module in {"__init__", "main"}:
            continue  # entrypoints/package markers are imported or executed
        # References anywhere except this file itself.
        refs = 0
        for other in files:
            if other == path:
                continue
            refs += len(re.findall(rf"\b{re.escape(module)}\b", texts_by_path[other]))
        if refs == 0:
            rel = os.path.relpath(path, start=os.path.dirname(app_root))
            findings.append(f"{rel}: module never imported anywhere (orphan)")
    return findings


def audit(root: str) -> list[str]:
    files = _collect_files(root)
    texts_by_path: dict[str, str] = {}
    trees: dict[str, ast.Module] = {}
    for path in files:
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
            texts_by_path[path] = text
            trees[path] = ast.parse(text, filename=path)
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue  # unparseable files are another audit's problem

    texts = list(texts_by_path.values())
    findings: list[str] = []

    # ── A. unreachable statements ──────────────────────────────────────────
    for path, tree in trees.items():
        rel = os.path.relpath(path, start=root)
        for node in ast.walk(tree):
            for body in _iter_bodies(node):
                for idx, stmt in enumerate(body[:-1]):
                    if isinstance(stmt, TERMINATORS):
                        dead = body[idx + 1]
                        findings.append(
                            f"{rel}:{dead.lineno}: unreachable code after "
                            f"{type(stmt).__name__.lower()} on line {stmt.lineno}"
                        )
                        break

    # ── B/C/D/E. unreferenced definitions ──────────────────────────────────
    for path, tree in trees.items():
        rel = os.path.relpath(path, start=root)
        if rel.split(os.sep)[0] not in REPORT_DIRS:
            continue
        for node, name, private in _defined_names(tree):
            refs = _count_references(name, texts)
            if refs > 1:
                continue
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                # A decorator is a registration: frameworks look the definition up
                # by reference (``@app.exception_handler(...)``,
                # ``@router.get(...)``, ``@pytest.fixture``), so the name appearing
                # only at its own ``def`` line is expected and NOT dead code.
                # This check must come first for private names too — the private
                # path below has no reference-count escape hatch, so a decorated
                # private handler (app/main.py's ``_timeout_handler``) would
                # otherwise be reported as dead.
                if node.decorator_list:
                    continue
                if private:
                    body = [
                        s for s in node.body
                        if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))
                    ]
                    if len(body) == 1 and isinstance(body[0], ast.Pass):
                        findings.append(
                            f"{rel}:{node.lineno}: private {type(node).__name__.lower()} "
                            f"'{name}' is a pass-only stub and is never referenced"
                        )
                    else:
                        findings.append(
                            f"{rel}:{node.lineno}: private {type(node).__name__.lower()} "
                            f"'{name}' is never referenced"
                        )
                else:
                    kind = "class" if isinstance(node, ast.ClassDef) else "function"
                    findings.append(
                        f"{rel}:{node.lineno}: public {kind} '{name}' is never "
                        "referenced and has no decorator"
                    )
            elif private:
                findings.append(
                    f"{rel}:{node.lineno}: constant '{name}' is assigned but never referenced"
                )

    # ── F. orphan modules ─────────────────────────────────────────────────
    app_root = os.path.join(root, "app") + os.sep
    findings.extend(_orphan_modules(app_root, files, texts_by_path))

    return sorted(set(findings))


def main(argv: list[str]) -> int:
    default_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    root = os.path.abspath(argv[0]) if argv else default_root
    if not os.path.isdir(root):
        print(f"Audit root not found: {root}", file=sys.stderr)
        return 2

    findings = audit(root)

    print("=" * 60)
    print("  Dead Code Audit")
    print(f"  scanning: {root}")
    print("=" * 60)
    if findings:
        print(f"\n  [FAIL]  {len(findings)} finding(s):\n")
        for f in findings:
            print(f"       • {f}")
        print("\n  Note: reference counting is whole-word and tree-wide, so a name\n"
              "  used only in a string, a test, or another script is NOT reported.\n"
              "  Each finding is worth a look — confirm before deleting.\n")
        print("=" * 60)
        return 1

    print("\n  [OK]  No dead code found")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
