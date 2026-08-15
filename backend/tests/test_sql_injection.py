"""
SQL-injection regression guard.

The codebase rule is that **every** asyncpg query is a plain string literal
with ``$1``/``$2``/... placeholders and the values passed as separate
arguments — parameterized, so the driver binds the values and never re-parses
them as SQL. The AST-based scanner in ``scripts/sql_injection_audit.py``
enforces this statically; this test:

* runs the scanner over the whole app tree (fails the suite if any
  non-parameterized query is introduced), and
* guards the guard — the scanner itself must flag every known dynamic-SQL
  form (f-string, concatenation, %-format, .format(), unverifiable variable)
  and accept the legitimate forms (DDL constants + ``$N`` placeholders).
"""
import os
import subprocess
import sys
import tempfile
import textwrap

BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
AUDIT_SCRIPT = os.path.join(BACKEND_DIR, "scripts", "sql_injection_audit.py")
APP_DIR = os.path.join(BACKEND_DIR, "app")


def _run_audit(target: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, AUDIT_SCRIPT, target],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,  # the audit exits non-zero on findings; tests assert returncode
    )


def _write(root: str, relpath: str, content: str) -> str:
    path = os.path.join(root, relpath)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)
    return path


def test_sql_injection_audit_is_clean():
    """The whole app tree must be parameterized (scanner exits 0)."""
    assert os.path.isfile(AUDIT_SCRIPT), f"audit script missing: {AUDIT_SCRIPT}"
    result = _run_audit(APP_DIR)
    assert result.returncode == 0, (
        "SQL injection audit failed:\n" + result.stdout + result.stderr
    )


def test_audit_accepts_legitimate_patterns():
    """DDL constants (same-module + relative import) and $N-parameterized
    queries are the sanctioned forms and must pass."""
    with tempfile.TemporaryDirectory() as tmp:
        _write(tmp, "app/database.py", 'USERS_TABLE_DDL = """CREATE TABLE IF NOT EXISTS users (id UUID PRIMARY KEY)"""\n')
        _write(tmp, "app/__init__.py", "")
        _write(tmp, "app/routes/__init__.py", "")
        _write(tmp, "app/routes/auth.py", textwrap.dedent("""\
            from ..database import USERS_TABLE_DDL

            async def ok(conn):
                await conn.execute(USERS_TABLE_DDL)
                await conn.fetchval("SELECT COUNT(*) FROM users")
                await conn.fetchrow("SELECT * FROM users WHERE id = $1", 1)
        """))
        result = _run_audit(os.path.join(tmp, "app"))
        assert result.returncode == 0, (
            "audit rejected legitimate patterns:\n" + result.stdout
        )


def test_audit_flags_every_dynamic_sql_form():
    """Guard the guard: each known dynamic-SQL vector must be flagged.

    The last vector (a function-local variable holding a query) is flagged by
    design: only module-level constants can be statically verified as plain
    literals, so an unverifiable variable fails closed.
    """
    vectors = [
        ('await conn.execute(f"SELECT * FROM users WHERE name = {x}")', "f-string"),
        ('await conn.fetchrow("SELECT * FROM users WHERE id = " + x)', "concatenat"),
        ('sql = "UPDATE users SET plan = %s" % x\n        await conn.execute(sql)', "format"),
        ('await conn.execute("DELETE FROM users WHERE id = {}".format(x))', "format"),
        ('sql = "SELECT * FROM users"\n        await conn.execute(sql)', "variable"),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        for i, (body, _needle) in enumerate(vectors):
            _write(tmp, f"p{i}.py", f"async def bad(conn, x):\n        {body}\n")
        result = _run_audit(tmp)
        assert result.returncode == 1, (
            "audit failed to flag dynamic SQL:\n" + result.stdout
        )
        for i, (_body, needle) in enumerate(vectors):
            assert needle in result.stdout, (
                f"vector {i} ({needle}) not flagged:\n{result.stdout}"
            )
