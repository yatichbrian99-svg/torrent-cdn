"""Security tests: log hygiene, secret handling, config gates, verification."""

from __future__ import annotations

import hashlib
import json
import os
import stat

import pytest

from app.ai.config import AISettings
from app.ai.errors import CryptoError
from app.ai.keyring import FileSecretStore, KeyRing, load_keyring
from app.ai.verification import VerificationSampler, resolve_executions
from app.ai.worker import EchoBackend


class TestSecretHandling:
    def test_keyring_file_must_be_0600(self, tmp_path):
        path = tmp_path / "keys.json"
        path.write_text(json.dumps(KeyRing.generate("c").to_secret()))
        os.chmod(path, 0o644)
        settings = AISettings(
            api_key_pepper="p",
            coordinator_key_file=str(path),
            environment="development",
        )
        with pytest.raises(CryptoError) as excinfo:
            load_keyring(settings)
        assert excinfo.value.code == "insecure_key_file"

    def test_keyring_saved_as_0600(self, tmp_path):
        path = tmp_path / "keys.json"
        store = FileSecretStore(path)
        store.save(KeyRing.generate("c").to_secret())
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert (
            load_keyring(
                AISettings(api_key_pepper="p", coordinator_key_file=str(path))
            ).active
            is not None
        )

    def test_keyring_can_be_injected_as_read_only_environment_secret(self, monkeypatch):
        # The platform secret manager injects the whole keyring; nothing is ever
        # written back to the environment.
        monkeypatch.setenv(
            "TCDN_AI_COORDINATOR_KEYRING_JSON",
            json.dumps(KeyRing.generate("injected").to_secret()),
        )
        settings = AISettings(
            api_key_pepper="p", coordinator_keyring_json="injected-by-platform"
        )
        ring = load_keyring(settings)
        assert ring.active.key_id
        from app.ai.keyring import EnvSecretStore

        with pytest.raises(CryptoError) as excinfo:
            EnvSecretStore().save({})
        assert excinfo.value.code == "read_only_secret_store"

    def test_production_requires_a_key_source(self):
        with pytest.raises(ValueError, match="COORDINATOR_KEY"):
            AISettings(
                environment="production",
                require_tls=True,
                allow_simulated_attestation=False,
                allow_dev_stub_backend=False,
                payment_provider="paystack",
                api_key_pepper="p",
            )

    def test_public_bundle_contains_no_private_material(self):
        ring = KeyRing.generate("c")
        document = ring.public_bundle()
        serialized = json.dumps(document)
        assert "private_key" not in serialized
        assert ring.active.private_key not in serialized

    def test_production_refuses_to_auto_generate_keys(self, tmp_path, monkeypatch):
        settings = AISettings(
            environment="production",
            require_tls=True,
            allow_simulated_attestation=False,
            allow_dev_stub_backend=False,
            payment_provider="paystack",
            api_key_pepper="p",
            coordinator_key_file=str(tmp_path / "missing.json"),
        )
        with pytest.raises(CryptoError) as excinfo:
            load_keyring(settings)
        assert excinfo.value.code == "missing_keyring"

    def test_dev_stub_backend_cannot_start_in_production(self):
        with pytest.raises(RuntimeError):
            EchoBackend(allow=False)


class TestProductionGates:
    """One case per refusal, each asserting *which* refusal fired.

    Asserting only ``ValueError`` would pass when the settings were rejected for
    an unrelated reason (a typo in the test itself, a second refusal masking the
    one under test), so every case names its own message.
    """

    @pytest.mark.parametrize(
        ("overrides", "refusal"),
        [
            ({"require_tls": False}, "TCDN_AI_REQUIRE_TLS must be true"),
            (
                {"allow_simulated_attestation": True},
                "TCDN_AI_ALLOW_SIMULATED_ATTESTATION must be false",
            ),
            (
                {"allow_dev_stub_backend": True},
                "TCDN_AI_ALLOW_DEV_STUB_BACKEND must be false",
            ),
            (
                {"payment_provider": "fake"},
                "TCDN_AI_PAYMENT_PROVIDER must not be 'fake'",
            ),
            ({"api_key_pepper": None}, "TCDN_AI_API_KEY_PEPPER must be set"),
            ({"coordinator_key_file": None}, "COORDINATOR_KEY_FILE"),
            (
                {"enable_tcdn_burn": True, "burn_legal_review_ack": False},
                "TCDN_AI_BURN_LEGAL_REVIEW_ACK",
            ),
        ],
    )
    def test_insecure_production_configs_are_rejected(self, overrides, refusal):
        base = {
            "environment": "production",
            "require_tls": True,
            "allow_simulated_attestation": False,
            "allow_dev_stub_backend": False,
            "payment_provider": "paystack",
            "api_key_pepper": "prod-pepper",
            "coordinator_key_file": "/run/secrets/keys.json",
        }
        base.update(overrides)
        with pytest.raises(ValueError, match=refusal):
            AISettings(**base)

    def test_a_complete_production_config_is_accepted(self):
        # The mirror of the cases above: without this, every one of them would
        # still pass if AISettings rejected production unconditionally.
        settings = AISettings(
            environment="production",
            require_tls=True,
            allow_simulated_attestation=False,
            allow_dev_stub_backend=False,
            payment_provider="paystack",
            api_key_pepper="prod-pepper",
            coordinator_key_file="/run/secrets/keys.json",
        )
        assert settings.is_production

    def test_memory_store_refuses_to_exist_in_production(self):
        from app.ai.store import MemoryStore

        production = AISettings(
            environment="production",
            require_tls=True,
            allow_simulated_attestation=False,
            allow_dev_stub_backend=False,
            payment_provider="paystack",
            api_key_pepper="prod-pepper",
            coordinator_key_file="/run/secrets/keys.json",
        )
        with pytest.raises(RuntimeError, match="not available in production"):
            MemoryStore(production)
        # …and only because of production, not because the class is broken.
        MemoryStore(AISettings(api_key_pepper="dev"))

    def test_split_must_sum_to_100(self):
        with pytest.raises(ValueError):
            AISettings(compute_share_pct=60, seeder_share_pct=10, platform_share_pct=20)


class TestStaticSafety:
    def test_no_interpolated_sql(self):
        import ast
        import pathlib

        import app.ai

        keywords = ("SELECT ", "INSERT ", "UPDATE ", "DELETE ", "DROP ")
        offenders: list[str] = []
        for path in pathlib.Path(app.ai.__file__).parent.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.JoinedStr):
                    continue
                literal = "".join(
                    value.value
                    for value in node.values
                    if isinstance(value, ast.Constant) and isinstance(value.value, str)
                ).upper()
                if any(keyword in literal for keyword in keywords):
                    offenders.append(f"{path.name}:{node.lineno}")
        assert offenders == [], f"interpolated SQL found: {offenders}"

    def test_no_dynamic_execution(self):
        import ast
        import pathlib

        import app.ai
        import tcdn_ai_sdk

        offenders: list[str] = []
        for package in (app.ai, tcdn_ai_sdk):
            for path in pathlib.Path(package.__file__).parent.glob("*.py"):
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                for node in ast.walk(tree):
                    if (
                        isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id in {"eval", "exec", "compile"}
                    ):
                        offenders.append(f"{path.name}:{node.lineno}")
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            if alias.name in {"pickle", "marshal", "shelve"}:
                                offenders.append(f"{path.name}:{node.lineno}")
        assert offenders == [], f"dynamic execution found: {offenders}"


class TestVerification:
    def test_sampling_is_deterministic(self):
        sampler = VerificationSampler(25, secret=b"unit-test-sampling-key")
        first = sampler.decide("job-abc")
        second = sampler.decide("job-abc")
        assert first == second
        assert (
            VerificationSampler(100, secret=b"unit-test-sampling-key")
            .decide("job-abc")
            .sampled
        )
        assert (
            not VerificationSampler(0, secret=b"unit-test-sampling-key")
            .decide("job-abc")
            .sampled
        )

    def test_sample_is_not_derivable_from_the_job_id(self):
        """A node holds the job id; knowing it must not predict the sample.

        The old scheme was ``sha256(public-salt || job_id)``, which any worker can
        recompute. This checks three things against the real sampler at the
        shipping 3 % rate: the rate is right, the public-salt construction has no
        predictive power over the jobs actually sampled, and a coordinator with a
        different secret disagrees on them.
        """
        sampler = VerificationSampler(3, secret=b"coordinator-only-key")
        job_ids = [f"job_{index:04d}" for index in range(2000)]
        sampled = [job for job in job_ids if sampler.decide(job).sampled]
        # ~3 % of 2000 = 60. Bounds are wide enough to absorb hash noise but
        # would fail if the rate were read as a percentage of 100 or ignored.
        assert 20 <= len(sampled) <= 120, f"3 % of 2000 sampled {len(sampled)} jobs"

        def public_salt_predicts(job: str) -> bool:
            digest = hashlib.sha256(
                f"tcdn-ai/verification/v1:{job}".encode()
            ).hexdigest()
            return int(digest[:8], 16) % 10_000 < 300

        guessed = sum(1 for job in sampled if public_salt_predicts(job))
        assert guessed < len(sampled) // 3, (
            "the public-salt construction still predicts the sample: "
            f"{guessed}/{len(sampled)}"
        )

        other = VerificationSampler(3, secret=b"a-different-coordinator")
        agreed = sum(1 for job in sampled if other.decide(job).sampled)
        assert agreed < len(sampled) // 3, (
            f"another key sampled {agreed}/{len(sampled)} of the same jobs"
        )

    def test_the_shipping_rate_is_three_percent_and_needs_a_secret(self):
        settings = AISettings(
            environment="development",
            api_key_pepper="unit-test-pepper-not-a-real-secret",
            coordinator_key_file=None,
        )
        assert settings.verification_sample_pct == 3
        # The key is coordinator-side and never empty: with no dedicated key the
        # pepper stands in, so a worker cannot recompute the bucket either way.
        assert settings.verification_sampling_secret()
        assert (
            AISettings(
                environment="development",
                api_key_pepper="p",
                verification_sample_key="dedicated",
                coordinator_key_file=None,
            ).verification_sampling_secret()
            != settings.verification_sampling_secret()
        )
        with pytest.raises(ValueError):
            VerificationSampler(3, secret=b"")

    def test_all_executions_agree(self):
        outcome = resolve_executions(
            job_id="job", outputs={"a": "h" * 64, "b": "h" * 64}
        )
        assert outcome.status == "agreed"
        assert not outcome.faulty_nodes

    def test_majority_tiebreak_identifies_faulty_node(self):
        outcome = resolve_executions(
            job_id="job",
            outputs={"a": "h" * 64, "b": "h" * 64, "c": "z" * 64},
        )
        assert outcome.status == "majority"
        assert outcome.faulty_nodes == ("c",)

    def test_no_majority_is_inconclusive(self):
        outcome = resolve_executions(
            job_id="job", outputs={"a": "a" * 64, "b": "b" * 64, "c": "c" * 64}
        )
        assert outcome.status == "inconclusive"
        assert not outcome.faulty_nodes

    @pytest.mark.asyncio
    async def test_service_penalises_and_suspends_faulty_nodes(
        self, ai_env, ai_worker_factory
    ):
        first = await ai_worker_factory()
        second = await ai_worker_factory()
        third = await ai_worker_factory()
        before = {
            node.node_id: node.reputation for node in await ai_env.store.list_nodes()
        }
        outcome = await ai_env.coordinator.record_verification(
            "job-verification",
            {
                first.identity.node_id: "a" * 64,
                second.identity.node_id: "a" * 64,
                third.identity.node_id: "b" * 64,
            },
        )
        assert outcome.status == "majority"
        assert outcome.faulty_nodes == (third.identity.node_id,)
        nodes = {node.node_id: node for node in await ai_env.store.list_nodes()}
        assert (
            nodes[third.identity.node_id].reputation
            == before[third.identity.node_id] - 20
        )
        assert any(
            entry["event"] == "node.verification_failed"
            for entry in ai_env.store.audit_log
        )

    def test_verification_decision_is_exposed_for_audit(self, ai_env):
        decision = ai_env.coordinator.verification_decision("job-xyz")
        assert 0 <= decision.bucket < 10_000
        assert decision.sampled is (
            decision.bucket < ai_env.settings.verification_sample_pct * 100
        )


class TestSimulationLabels:
    """Row 1.11: every simulated component says so where a caller can see it.

    "Labelled in the docs" is not enough: an API consumer never reads our docs
    before trusting a boolean. These assertions are on the surfaces a client or
    operator actually reads — worker health, the attestation verdict and the
    transcript provenance.
    """

    @pytest.mark.asyncio
    async def test_worker_health_names_the_stub_backend(self, ai_worker_factory):
        from starlette.testclient import TestClient

        from app.ai.worker import create_worker_app

        agent = await ai_worker_factory()
        with TestClient(create_worker_app(agent)) as client:
            health = client.get("/health").json()
        assert health["backend"] == "dev-stub"
        assert health["attested"] is False
        assert health["confidentiality"] == "none"
        assert health["status"] == "ok"

    @pytest.mark.asyncio
    async def test_transcript_from_the_stub_is_marked_simulated(
        self, ai_worker_factory
    ):
        agent = await ai_worker_factory()
        assert agent.backend.transcript_source == "simulated"

    def test_simulated_attestation_verdict_is_labelled_in_every_field(self):
        from app.ai.attestation import SimulatedVerifier
        from app.ai.models import AttestationEvidencePayload, AttestationPlatform

        verifier = SimulatedVerifier(allow=True)
        verdict = verifier.verify(
            AttestationEvidencePayload(
                node_id="node_x",
                platform=AttestationPlatform.SEV_SNP,
                nonce="n" * 16,
                session_key_sha256="a" * 64,
                report_b64="c" * 16,
                issued_at=1,
            ),
            now=1,
        )
        assert verdict.status == "simulated"
        assert verdict.verifier == "simulated"
        assert verdict.measurement == "simulated-measurement"
        assert verdict.tcb == "simulated"
        # It borrows ATTESTED_TEE for the type, which is exactly why status and
        # verifier must stay visible: nothing downstream may read the tier alone
        # as proof of attestation.
        assert "simulated" in (verdict.reason or "").lower()

    def test_api_error_bodies_never_claim_attestation(self, ai_env):
        """The refusal path a client actually sees: a plain, honest error."""
        from app.ai.errors import AttestationError

        assert AttestationError("attestation is required for this tier").code == (
            "attestation_failed"
        )


class TestSecurityDocCitations:
    """Row 5.10: the OWASP tables must point at tests that exist.

    A control table whose "Test" column names a test that was renamed or never
    written is worse than no table: it reads as evidence. This test parses the
    three OWASP tables in ``docs/tcdn-ai/SECURITY.md`` and resolves every
    citation against the test tree, so the doc cannot drift from the suite.
    """

    def test_every_cited_test_exists(self):
        import pathlib
        import re

        repo = pathlib.Path(__file__).resolve().parents[2]
        document = (repo / "docs/tcdn-ai/SECURITY.md").read_text(encoding="utf-8")
        start = document.index("## 4. OWASP Top 10 (2021)")
        end = document.index("## 7. Additional controls")
        section = document[start:end]
        tests_dir = repo / "backend/tests"
        sources = "\n".join(
            path.read_text(encoding="utf-8", errors="ignore")
            for path in tests_dir.glob("test_*.py")
        )

        cited: set[tuple[str, str]] = set()
        for cell in re.findall(r"`([^`]+)`", section):
            for part in cell.split(","):
                match = re.match(
                    r"([A-Za-z_][\w]*)(?:::([A-Za-z0-9_]+))?$", part.strip()
                )
                if not match:
                    continue
                name, method = match.group(1), match.group(2) or ""
                if name.startswith(("Test", "test_")):
                    cited.add((name, method))

        assert cited, "the OWASP tables cite no tests at all"
        missing: list[str] = []
        for name, method in sorted(cited):
            if name.startswith("test_"):
                found = f"def {name}(" in sources
            elif method:
                block = re.search(
                    rf"class {name}\b.*?(?=\nclass |\Z)", sources, re.DOTALL
                )
                found = bool(block and f"def {method}(" in block.group(0))
            else:
                found = f"class {name}" in sources
            if not found:
                missing.append(f"{name}::{method}" if method else name)
        assert missing == [], f"OWASP tables cite tests that do not exist: {missing}"


class TestDisclosureDocs:
    """Rows 6.3 and 11.1: the limitations are stated where a buyer reads them.

    Both rows are documentation claims, so the check is on the documents. It is
    deliberately narrow: a few load-bearing sentences, not a prose test.
    """

    def _docs(self) -> tuple[str, str]:
        import pathlib

        repo = pathlib.Path(__file__).resolve().parents[2]
        return (
            (repo / "docs/tcdn-ai/LIMITATIONS.md").read_text(encoding="utf-8"),
            (repo / "docs/tcdn-ai/API.md").read_text(encoding="utf-8"),
        )

    def test_verification_is_described_as_probabilistic_with_its_limits(self):
        limitations, _ = self._docs()
        start = limitations.index("## Result verification is probabilistic")
        section = limitations[
            start : limitations.index("## Tier and visibility", start)
        ]
        assert "probabilistic, not a proof" in section
        assert "never sampled" in section or "95 %" in section
        # The section must name where the second run does and does not happen:
        # gateway mode dispatches it, SDK mode cannot (the coordinator has no
        # plaintext), so the guarantee cannot be read as universal.
        assert "dispatched automatically in gateway mode" in section
        assert "not_dispatched_sdk_mode" in section
        # …and the sampling weakness: predictable to the node, decided before
        # any commitment.
        assert "predictable to the node" in section

    def test_tiers_are_disclosed_with_gateway_visibility(self):
        limitations, api = self._docs()
        start = limitations.index("## Tier and visibility disclosure")
        section = limitations[start:]
        assert "Not available in this build" in section
        assert "standard is explicitly non-confidential" in section
        assert "gateway" in section.lower()
        # …and the API doc points a client at the same disclosure.
        assert "prompt_visibility" in api
        assert "LIMITATIONS.md" in api.split("Status mapping", 1)[1]
