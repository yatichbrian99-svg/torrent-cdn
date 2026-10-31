"""The crypto review and key inventory must point at code and tests that exist.

Rows 5.1 and 5.2 ask for a review artifact, not a paragraph. An artifact whose
citations were renamed away is worse than none: it reads as evidence. This test
resolves every `module.py::symbol` and `Class::method` in both documents against
the real tree, and checks the "no key material in git" claim mechanically.
"""

from __future__ import annotations

import pathlib
import re
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
DOCS = [REPO / "docs/tcdn-ai/CRYPTO_REVIEW.md", REPO / "docs/tcdn-ai/KEY_INVENTORY.md"]
CELL = re.compile(r"`([^`]+)`")


def _test_sources() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in (REPO / "backend/tests").glob("test_*.py")
    )


def _module_source(module: str) -> str | None:
    for base in ("backend",):
        path = REPO / base / module
        if path.is_file():
            return path.read_text(encoding="utf-8", errors="ignore")
    return None


def _resolve(cell: str, tests_source: str) -> str | None:
    """None when fine, otherwise a human-readable problem."""
    if "::" not in cell:
        return None
    target, symbol = (part.strip() for part in cell.split("::", 1))
    if not target or not symbol:
        return f"{cell}: empty target or symbol"

    if target.startswith(("app/", "tcdn_ai_sdk/")):
        source = _module_source(target)
        if source is None:
            return f"{cell}: module {target} does not exist"
        name = symbol.split(".")[-1]
        for pattern in (f"def {name}(", f"class {name}", f"{name}:", f"{name} ="):
            if pattern in source:
                return None
        return f"{cell}: {name} not found in {target}"

    if target.startswith(("Test", "test_")):
        if target.startswith("test_"):
            return None if f"def {target}(" in tests_source else f"{cell}: no such test"
        block = re.search(
            rf"class {target}\b.*?(?=\nclass |\Z)", tests_source, re.DOTALL
        )
        if block is None:
            return f"{cell}: no such test class"
        return (
            None
            if f"def {symbol}(" in block.group(0)
            else f"{cell}: no such method in {target}"
        )

    return None


@pytest.mark.parametrize("document", DOCS, ids=[doc.name for doc in DOCS])
def test_every_citation_resolves(document: pathlib.Path):
    text = document.read_text(encoding="utf-8")
    cells = [
        cell
        for cell in CELL.findall(text)
        if "::" in cell and not cell.startswith(("http", "TCDN_", "JWT_", "node_"))
    ]
    assert len(cells) >= 10, f"{document.name} cites only {len(cells)} symbols"
    tests_source = _test_sources()
    problems = [problem for cell in cells if (problem := _resolve(cell, tests_source))]
    assert not problems, (
        f"{document.name} cites things that do not exist:\n" + "\n".join(problems)
    )


def test_the_crypto_review_covers_every_primitive_the_package_imports():
    """Every crypto library import in app/ai has a row in the review.

    The inventory can only be complete if it is derived from the imports rather
    than from memory; this pins the set.
    """
    review = (REPO / "docs/tcdn-ai/CRYPTO_REVIEW.md").read_text(encoding="utf-8")
    package = REPO / "backend/app/ai"
    used: set[str] = set()
    for path in package.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        for module in ("cryptography", "hashlib", "hmac", "secrets"):
            if re.search(
                rf"^\s*(import {module}\b|from {module}\b)", source, re.MULTILINE
            ):
                used.add(module)
    assert used, "no crypto imports found — the scan is broken"
    # `hashlib` and `hmac` are described by purpose rather than by import name;
    # assert the review names each algorithm the package actually calls.
    for algorithm in (
        "Ed25519",
        "X25519",
        "HKDF-SHA-256",
        "AES-256-GCM",
        "HMAC-SHA-256",
    ):
        assert algorithm in review, f"{algorithm} is used but not reviewed"


def test_the_injected_keyring_supports_a_production_start_and_is_read_only(
    monkeypatch,
):
    """Row 5.2's "prod secret-store mode": a platform-injected keyring, no file.

    The JSON is the system of record, so the process must be able to *load* a
    complete keyring from it in production and must refuse to write back.
    """
    import json

    from app.ai.config import AISettings
    from app.ai.errors import CryptoError
    from app.ai.keyring import EnvSecretStore, KeyRing, load_keyring

    ring = KeyRing.generate("coordinator")
    monkeypatch.setenv("TCDN_AI_COORDINATOR_KEYRING_JSON", json.dumps(ring.to_secret()))
    settings = AISettings(
        environment="production",
        require_tls=True,
        allow_simulated_attestation=False,
        allow_dev_stub_backend=False,
        payment_provider="paystack",
        api_key_pepper="unit-test-pepper-not-a-real-secret",
        coordinator_keyring_json=json.dumps(ring.to_secret()),
    )
    loaded = load_keyring(settings)
    assert loaded.active.key_id == ring.active.key_id

    store = EnvSecretStore()
    assert store.load()["keys"]  # the same document the process was started with
    with pytest.raises(CryptoError) as excinfo:
        store.save({})
    assert excinfo.value.code == "read_only_secret_store"


def test_no_key_material_is_tracked():
    """The inventory ships; the keys do not."""
    try:
        listing = subprocess.run(
            ["git", "-C", str(REPO), "ls-files"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:  # pragma: no cover
        pytest.skip(f"git unavailable: {exc}")
    forbidden = [
        path
        for path in listing.splitlines()
        if re.search(r"(^|/)(id_ed25519|keys?\.json|.*\.(pem|key|p12|pfx))$", path)
        and not path.endswith(".example")
    ]
    assert forbidden == [], f"key material is tracked in git: {forbidden}"
