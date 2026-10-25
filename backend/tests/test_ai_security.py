"""Security tests: log hygiene, secret handling, config gates, verification."""

from __future__ import annotations

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
        assert load_keyring(
            AISettings(api_key_pepper="p", coordinator_key_file=str(path))
        ).active is not None

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
    @pytest.mark.parametrize(
        "overrides",
        [
            {"require_tls": False},
            {"allow_simulated_attestation": True},
            {"allow_dev_stub_backend": True},
            {"payment_provider": "fake"},
            {"api_key_pepper": None},
            {"coordinator_key_file": None},
        ],
    )
    def test_insecure_production_configs_are_rejected(self, overrides):
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
        with pytest.raises(ValueError):
            AISettings(**base)

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
        sampler = VerificationSampler(25)
        first = sampler.decide("job-abc")
        second = sampler.decide("job-abc")
        assert first == second
        assert VerificationSampler(100).decide("job-abc").sampled
        assert not VerificationSampler(0).decide("job-abc").sampled

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
    async def test_service_penalises_and_suspends_faulty_nodes(self, ai_env, ai_worker_factory):
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
        assert nodes[third.identity.node_id].reputation == before[third.identity.node_id] - 20
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
