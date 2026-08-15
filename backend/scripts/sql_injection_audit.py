#!/usr/bin/env python3
"""
SQL Injection Static Audit — CI/CD compatible.

Parses every ``.py`` file under ``backend/app`` with Python's ``ast`` module
(stdlib — no third-party dependencies) and fails the build on any database
call whose SQL is not a plain string literal, or any dynamically-built string
that contains SQL.

Why this exists
---------------
The codebase rule is: **every** asyncpg query is a plain string literal with
``$1``/``$2``/... placeholders and the values passed as separate arguments.
That makes injection impossible — the driver binds the values and never
re-parses them as SQL. This audit is the guard that keeps the rule from
regressing: an f-string, ``.format()``, ``%-format``, or string-concatenated
query is flagged with a file:line reference, whether it is passed to a DB call
directly or assembled by a helper far from the call site.

Gates (all hard-fail):

* **Gate A — DB call sites:** any call to an asyncpg SQL method (``execute`` /
  ``fetch`` / ``fetchrow`` / ``fetchval`` / ``executemany`` / ``prepare`` /
  ``copy_*``) whose first argument is not a provably-plain string literal.
  A ``Name`` argument is accepted only when it resolves (in this module, or
  through a ``from X import NAME`` / ``import X`` chain) to a module-level
  constant whose every top-level assignment is a plain string literal — e.g.
  the ``*_TABLE_DDL`` constants in ``database.py``. Anything else — an f-string,
  a local variable, an interpolated constant — is flagged.
* **Gate B — dynamic strings:** any f-string, ``.format()``, ``%-format``, or
  ``+``-concatenation expression whose value contains an unambiguous SQL marker
  (``SELECT `` / ``INSERT INTO`` / ``UPDATE `` / ``DELETE FROM`` /
  ``CREATE TABLE`` ...), which catches a query assembled in a helper far from
  the call site.

Note: implicit adjacent string literals (``"SELECT ..." "FROM ..."``) are
folded into a single Constant by the parser, so they are correctly accepted.

Exit code 0 = clean, non-zero = findings (printed with file:line).
"""
from __future__ import annotations

import ast
import os
import sys

# asyncpg connection methods whose first positional argument is SQL text.
# (The app uses execute/fetchrow/fetchval/fetch; the rest are guarded so a new
# method can't slip past the audit.)
DB_SQL_METHODS = {
    "execute",
    "executemany",
    "fetch",
    "fetchrow",
    "fetchval",
    "prepare",
    "copy_records_to_table",
    "copy_from_table",
    # asyncpg COPY helpers that take SQL text (as opposed to copy_*_table,
    # whose first arg is a table name).
    "copy_from_query",
    "copy_to_query",
}

# Unambiguous SQL markers for Gate B. Each is uppercase + trailing space/paren
# so prose ("we update the record", "DELETE FROM the list") can't false-positive
# — a lowercase word doesn't match, and "UPDATE" without a space doesn't match.
SQL_MARKERS = (
    "SELECT ",
    "INSERT INTO",
    "UPDATE ",
    "DELETE FROM",
    "CREATE TABLE",
    "CREATE INDEX",
    "CREATE UNIQUE INDEX",
    "ALTER TABLE",
    "DROP TABLE",
    "TRUNCATE ",
)

_EXPECTED = (
    "expected a plain string literal with $1/$2 placeholders and the values "
    "passed as separate arguments (asyncpg parameterization)"
)


def _is_plain_sql_literal(node: ast.AST) -> bool:
    """True only for a string Constant. Implicit-adjacent literals are folded
    into a single Constant by the parser, so they count as plain too."""
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _contains_sql_marker(text: str) -> bool:
    upper = text.upper()
    return any(marker in upper for marker in SQL_MARKERS)


def _dynamic_string_kind(node: ast.AST) -> str:
    """Describe the dynamic-string expression kind for the finding message."""
    if isinstance(node, ast.JoinedStr):
        return "f-string"
    if isinstance(node, ast.BinOp):
        if isinstance(node.op, ast.Mod):
            return "%-formatting"
        if isinstance(node.op, ast.Add):
            return "string concatenation (+)"
    if isinstance(node, ast.Call):
        fn = node.func
        if isinstance(fn, ast.Attribute) and fn.attr == "format":
            return ".format()"
    if isinstance(node, ast.Name):
        return f"variable '{node.id}'"
    return type(node).__name__


# ── module-level constant resolution (Gate A's Name handling) ───────────────

def _module_dotted(path: str, root: str) -> str:
    """Dotted module path for a file under ``root`` (e.g. app/routes/auth.py
    → app.routes.auth). Mirrors Python's package layout (dirs up to ``root``)."""
    rel_dir = os.path.relpath(os.path.dirname(path), root)
    mod = os.path.splitext(os.path.basename(path))[0]
    if rel_dir == ".":
        return mod
    return ".".join(rel_dir.split(os.sep) + [mod])


def _resolve_import_target(current_module: str, node: ast.ImportFrom) -> str | None:
    """Resolve a ``from .x import y`` / ``from ..x import y`` to a dotted module."""
    if not node.module:
        return None
    level = node.level or 0
    pkg = current_module.split(".")[:-1]
    if level == 0:
        base: list[str] = []
    else:
        drop = level - 1
        if drop > len(pkg):
            return None  # relative import escapes the app tree — can't verify
        base = pkg[: len(pkg) - drop]
    return ".".join(base + node.module.split("."))


def _collect_module_info(tree: ast.Module, dotted: str) -> tuple[dict, dict]:
    """Return (local_constants, imports) for a module:
    - local_constants: {name: True iff EVERY top-level assignment of that name
      is a plain string Constant}
    - imports: {bound_name: dotted module it refers to}
    """
    local: dict[str, bool] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                name = node.targets[0].id
                # AND-accumulate: one dynamic assignment poisons the name.
                local[name] = local.get(name, True) and _is_plain_sql_literal(node.value)
    imports: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            target = _resolve_import_target(dotted, node)
            for alias in node.names:
                if alias.name != "*" and target:
                    imports[alias.asname or alias.name] = target
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    imports[alias.asname] = alias.name
                else:
                    imports[alias.name.split(".")[0]] = alias.name
    return local, imports


# ── the audit ───────────────────────────────────────────────────────────────

def audit_tree(root: str) -> list[str]:
    """Recursively audit every .py file under ``root``. Returns findings."""
    findings: list[str] = []

    py_files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in {"__pycache__", ".venv", "node_modules"}]
        for name in sorted(filenames):
            if name.endswith(".py"):
                py_files.append(os.path.join(dirpath, name))

    # Pass 0: parse everything once; a parse error is itself a finding.
    trees: dict[str, ast.Module] = {}
    for path in py_files:
        try:
            with open(path, encoding="utf-8") as f:
                trees[path] = ast.parse(f.read(), filename=path)
        except (OSError, SyntaxError) as e:
            findings.append(f"{path}: could not parse — {e}")

    # Pass 1: module-level constant + import info for every module.
    dotted_of: dict[str, str] = {p: _module_dotted(p, root) for p in trees}
    local_const: dict[str, dict] = {}
    imports: dict[str, dict] = {}
    for path, tree in trees.items():
        d = dotted_of[path]
        local_const[d], imports[d] = _collect_module_info(tree, d)
    dotted_to_path = {d: p for p, d in dotted_of.items()}

    # Name resolution with memoization + cycle guard.
    _resolved: dict[tuple[str, str], bool] = {}

    def _resolve_name(mod: str, name: str, seen: frozenset) -> bool:
        key = (mod, name)
        if key in _resolved:
            return _resolved[key]
        if mod in seen:
            return False
        cmap = local_const.get(mod, {})
        if name in cmap:
            val = cmap[name]
            _resolved[key] = val
            return val
        target = imports.get(mod, {}).get(name)
        if target and target in dotted_to_path:
            val = _resolve_name(target, name, seen | {mod})
            _resolved[key] = val
            return val
        _resolved[key] = False
        return False

    def _sql_arg_is_safe(arg: ast.AST, mod: str) -> bool:
        if _is_plain_sql_literal(arg):
            return True
        if isinstance(arg, ast.Name):
            return _resolve_name(mod, arg.id, frozenset())
        if isinstance(arg, ast.Attribute) and isinstance(arg.value, ast.Name):
            # module.NAME style — resolve the module via the imports map.
            target = imports.get(mod, {}).get(arg.value.id)
            if target and target in dotted_to_path:
                return _resolve_name(target, arg.attr, frozenset())
        return False

    # Pass 2: walk every module's AST for DB calls + dynamic SQL strings.
    for path, tree in trees.items():
        mod = dotted_of[path]

        class _Finder(ast.NodeVisitor):
            def visit_Call(self, node: ast.Call) -> None:
                self.generic_visit(node)
                if not isinstance(node.func, ast.Attribute):
                    return
                attr = node.func.attr
                if attr in DB_SQL_METHODS:
                    if node.args and not _sql_arg_is_safe(node.args[0], mod):
                        findings.append(
                            f"{path}:{node.lineno}: .{attr}() called with "
                            f"{_dynamic_string_kind(node.args[0])} SQL — {_EXPECTED}"
                        )
                elif attr == "format":
                    base = node.func.value
                    if _is_plain_sql_literal(base) and _contains_sql_marker(base.value):
                        findings.append(
                            f"{path}:{node.lineno}: .format() applied to SQL text — {_EXPECTED}"
                        )

            def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
                self.generic_visit(node)
                # Gate B: an f-string whose (constant) value contains SQL.
                # Non-literal interpolations are substituted with a space so the
                # marker scan is conservative (never a false positive).
                try:
                    value = "".join(
                        v.value if isinstance(v, ast.Constant) else " "
                        for v in node.values
                    )
                except Exception:
                    value = ""
                if _contains_sql_marker(value):
                    findings.append(
                        f"{path}:{node.lineno}: f-string contains SQL — {_EXPECTED}"
                    )

            def visit_BinOp(self, node: ast.BinOp) -> None:
                self.generic_visit(node)
                # Gate B: %-formatting or + concatenation applied to a
                # SQL-literal operand. ("text" % x on non-SQL text is fine.)
                if not isinstance(node.op, (ast.Mod, ast.Add)):
                    return
                for operand in (node.left, node.right):
                    if _is_plain_sql_literal(operand) and _contains_sql_marker(operand.value):
                        kind = "%-formatting" if isinstance(node.op, ast.Mod) else "string concatenation"
                        findings.append(
                            f"{path}:{node.lineno}: {kind} applied to SQL text — {_EXPECTED}"
                        )
                        return

        _Finder().visit(tree)

    return findings


def main(argv: list[str]) -> int:
    default_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "app"))
    root = os.path.abspath(argv[0]) if argv else default_root
    if not os.path.isdir(root):
        print(f"Audit root not found: {root}", file=sys.stderr)
        return 2

    findings = audit_tree(root)

    print("=" * 60)
    print("  SQL Injection Static Audit")
    print(f"  scanning: {root}")
    print("=" * 60)
    if findings:
        print(f"\n  [FAIL]  {len(findings)} finding(s):\n")
        for f in findings:
            print(f"       • {f}")
        print(
            "\n  Fix: pass the SQL as a plain string literal with $1/$2\n"
            "       placeholders and the values as separate arguments — the\n"
            "       driver binds them and can never re-parse them as SQL.\n"
        )
        print("=" * 60)
        return 1
    print("\n  [OK]  ALL DB CALLS PARAMETERIZED — no SQL injection vectors found")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
