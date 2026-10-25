"""Part 3 self-check: import and exercise the real reference surface of
:mod:`app.ai.verification2` plus the :mod:`app.ai.verifier_sign` signing layer.

This is a fast, deterministic in-process check. It does not require a running
service, a database, network access, or a GPU. It is meant to confirm that the
reference implementation is internally consistent and that the explicitly named
spec surface (``verifier_sign::ed25519_sign``, ``VerificationVoucher``) is wired
in and importable exactly as the spec describes.
"""

from __future__ import annotations

import hashlib
import time

import pytest

from app.ai import crypto
from app.ai.transcripts import EncryptedTranscriptStore, TranscriptRecorder, prompt_hash
from app.ai.verification2 import (
    CalibrationRegistry,
    ChallengeVerificationError,
    PenaltyRecord,
    StubVerifierRescorer,
    TranscriptRecord,
    Verdict,
    VerdictType,
    VerificationReceiptExt,
    VerificationRng,
    VerificationStatistics,
    build_transcript_root,
    compare_sequence,
    has_canary,
    penalty_accounts_for_destiny,
    sign_challenge_response,
    sign_verifier_verdict,
    statistics_digest,
    transcript_proof,
    verify_challenge_response,
)
from app.ai.verifier_sign import (
    VerificationVoucher,
    VerificationVoucherDomain,
    VoucherVerificationError,
    ed25519_sign,
    verify_voucher,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _deterministic_secret(seed: str = "part3-self-check") -> bytes:
    return hashlib.sha256(seed.encode()).digest()


def _simple_records(count: int, *, base_logprob: float = -1.0) -> list[TranscriptRecord]:
    records = []
    for i in range(count):
        top_k = ((i, base_logprob), (i + 1, base_logprob - 1.0))
        records.append(TranscriptRecord(
            position=i,
            token_id=i,
            chosen_logprob=base_logprob - i * 0.01,
            top_k=top_k,
        ))
    return records


# Real Ed25519 material for this in-process test. Generated per run (like every
# other signing test in the repo) so the tests exercise the actual primitive
# rather than a placeholder string.
_TEST_KEY = crypto.generate_ed25519()
_TEST_PRIVATE_B64 = _TEST_KEY["private_key"]
_TEST_PUBLIC_B64 = _TEST_KEY["public_key"]


# ---------------------------------------------------------------------------
# Transcript commitment (milestone 1 surface)
# ---------------------------------------------------------------------------

class TestTranscriptCommitment:
    def test_build_root_and_proof(self):
        records = _simple_records(4)
        root = build_transcript_root(records)
        assert isinstance(root, str) and len(root) == 64
        assert all(c.isdigit() or c in "abcdef" for c in root)

        from app.ai.shards import verify_merkle_proof

        for i in range(len(records)):
            proof = transcript_proof(records, i)
            assert verify_merkle_proof(records[i].leaf_hex(), proof, root) is True

        tampered = _simple_records(4)
        object.__setattr__(tampered[1], "token_id", 999)
        assert verify_merkle_proof(
            tampered[1].leaf_hex(), transcript_proof(records, 1), root
        ) is False

    def test_contiguous_positions_required(self):
        bad = [
            TranscriptRecord(position=0, token_id=1, chosen_logprob=-1.0, top_k=((1, -1.0),)),
            TranscriptRecord(position=2, token_id=2, chosen_logprob=-1.0, top_k=((2, -1.0),)),
        ]
        with pytest.raises(ValueError, match="contiguous"):
            build_transcript_root(bad)

    def test_empty_records_rejected(self):
        with pytest.raises(ValueError):
            build_transcript_root([])

    def test_leaf_is_deterministic(self):
        r = TranscriptRecord(position=0, token_id=7, chosen_logprob=-1.23, top_k=((7, -1.23), (8, -2.0)))
        a = r.leaf_hex()
        b = r.leaf_hex()
        assert a == b
        assert len(a) == 64


# ---------------------------------------------------------------------------
# Receipt extension fields (milestone 1 surface, additive)
# ---------------------------------------------------------------------------

class TestReceiptExtension:
    def _ext(self, records=None):
        return VerificationReceiptExt.build(
            receipt_id="receipt-1",
            engine="vllm",
            engine_version="0.6.3",
            dtype="bfloat16",
            quantization="none",
            gpu_class="a100-80g",
            sampling_params={"temperature": 0.7, "top_p": 0.9, "seed": 12345},
            prompt_hash=prompt_hash("job-1", [{"role": "user", "content": "hi"}]),
            transcript_records=records or _simple_records(3),
        )

    def test_build_produces_the_receipt_extension(self):
        records = _simple_records(3)
        ext = self._ext(records)
        assert ext["receipt_id"] == "receipt-1"
        assert ext["transcript_root"] == build_transcript_root(records)
        assert ext["token_count"] == 3
        assert ext["engine"] == "vllm"
        assert ext["engine_version"] == "0.6.3"
        assert ext["dtype"] == "bfloat16"
        assert ext["quantization"] == "none"
        assert ext["gpu_class"] == "a100-80g"
        assert ext["transcript_source"] == "engine"

    def test_extract_returns_none_for_missing(self):
        assert VerificationReceiptExt.extract({"job_id": "x"}) is None

    def test_round_trip_token_count(self):
        payload = {"verification": self._ext(_simple_records(5))}
        assert VerificationReceiptExt.token_count(payload) == 5
        assert VerificationReceiptExt.transcript_root(payload) == build_transcript_root(
            _simple_records(5)
        )

    def test_receipt_id_is_required_and_source_is_bounded(self):
        records = _simple_records(2)
        with pytest.raises(ValueError, match="receipt_id"):
            VerificationReceiptExt.build(
                receipt_id="", engine="vllm", engine_version="1", dtype="f16",
                quantization="none", gpu_class="g", sampling_params={},
                prompt_hash="0" * 64, transcript_records=records,
            )
        with pytest.raises(ValueError, match="transcript_source"):
            VerificationReceiptExt.build(
                receipt_id="r", engine="vllm", engine_version="1", dtype="f16",
                quantization="none", gpu_class="g", sampling_params={},
                prompt_hash="0" * 64, transcript_records=records,
                transcript_source="fabricated",
            )

    def test_prompt_hash_is_deterministic_and_job_bound(self):
        messages = [{"role": "user", "content": "CANARY-pro3-prompt"}]
        first = prompt_hash("job-1", messages)
        assert first == prompt_hash("job-1", messages)
        assert first != prompt_hash("job-2", messages)
        assert "CANARY" not in first
        assert len(first) == 64

    def test_receipt_payload_accepts_the_extension(self):
        from app.ai.models import ReceiptPayload

        payload = ReceiptPayload(
            job_id="job-1",
            node_id="node-1",
            account_id="account-1",
            model="llama-3.1-8b-instruct",
            manifest_hash="a" * 64,
            input_tokens=10,
            output_tokens=3,
            output_sha256="b" * 64,
            started_at=1,
            finished_at=2,
            verification=self._ext(),
        )
        assert payload.verification is not None
        assert payload.verification.token_count == 3


# ---------------------------------------------------------------------------
# Coordinator randomness: sampling + challenge derivation
# ---------------------------------------------------------------------------

class TestCoordinatorRandomness:
    def test_sample_decision_is_bounded_and_deterministic(self):
        secret = _deterministic_secret()
        rng = VerificationRng(secret)
        root = build_transcript_root(_simple_records(5))
        a = rng.sample_decision("job-a", root, rate_per_10k=300)
        b = rng.sample_decision("job-a", root, rate_per_10k=300)
        assert a == b
        assert isinstance(a, bool)

        # rate out of range rejected
        with pytest.raises(ValueError, match="rate_per_10k"):
            rng.sample_decision("job-a", root, rate_per_10k=10001)

    def test_challenge_indices_are_distinct_and_bounded(self):
        secret = _deterministic_secret()
        rng = VerificationRng(secret)
        root = build_transcript_root(_simple_records(200))
        idx = rng.challenge_indices("job-b", root, token_count=200)
        assert 8 <= len(idx) <= 256
        assert len(idx) == len(set(idx))
        assert all(0 <= i < 200 for i in idx)

    def test_challenge_indices_empty_for_zero_tokens(self):
        secret = _deterministic_secret()
        rng = VerificationRng(secret)
        assert rng.challenge_indices("job-z", "root", token_count=0) == []

    def test_challenge_indices_minimum_respected_for_small_output(self):
        secret = _deterministic_secret()
        rng = VerificationRng(secret)
        root = build_transcript_root(_simple_records(10))
        idx = rng.challenge_indices("job-s", root, token_count=10, minimum=8)
        assert len(idx) == 8


# ---------------------------------------------------------------------------
# Challenge response verification (deterministic violations, milestone 2)
# ---------------------------------------------------------------------------

class TestChallengeResponse:
    def test_valid_response_opens_matching_records(self):
        secret = _deterministic_secret()
        rng = VerificationRng(secret)
        records = _simple_records(20)
        root = build_transcript_root(records)
        indices = rng.challenge_indices("job-c", root, token_count=20)
        opened = {r.position: r for r in records}

        response_payload = {
            "responses": [
                {
                    "position": i,
                    "proof": transcript_proof(records, i),
                }
                for i in indices
            ],
        }
        opened_records = verify_challenge_response(
            response_payload,
            transcript_root=root,
            records=opened,
            node_public_key=_TEST_PUBLIC_B64,
            job_id="job-c",
        )
        assert len(opened_records) == len(indices)
        assert {r.position for r in opened_records} == set(indices)

    def test_bad_proof_is_deterministic_violation(self):
        records = _simple_records(5)
        root = build_transcript_root(records)
        fake_proof = [{"path": "bad", "hash": "bad"}]
        response_payload = {"responses": [{"position": 0, "proof": fake_proof}]}
        with pytest.raises(ChallengeVerificationError) as exc_info:
            verify_challenge_response(
                response_payload,
                transcript_root=root,
                records={r.position: r for r in records},
                node_public_key=_TEST_PUBLIC_B64,
                job_id="job-bad",
            )
        assert exc_info.value.code == "proof_mismatch"

    def test_missing_record_is_deterministic_violation(self):
        secret = _deterministic_secret()
        rng = VerificationRng(secret)
        root = build_transcript_root(_simple_records(5))
        indices = rng.challenge_indices("job-m", root, token_count=5)
        remote_index = indices[0] + 100  # not in our records
        response_payload = {"responses": [{"position": remote_index, "proof": []}]}
        with pytest.raises(ChallengeVerificationError) as exc_info:
            verify_challenge_response(
                response_payload,
                transcript_root=root,
                records={r.position: r for r in _simple_records(5)},
                node_public_key=_TEST_PUBLIC_B64,
                job_id="job-m",
            )
        assert exc_info.value.code == "challenge_open_failure"


# ---------------------------------------------------------------------------
# Tolerance profiles + calibration registry
# ---------------------------------------------------------------------------

class TestToleranceProfiles:
    def test_default_profile_has_all_fields(self):
        p = CalibrationRegistry().default()
        assert p.model_family == "unknown"
        assert p.profile_version == "stub-v0"
        assert p.chosen_logprob_abs_tolerance > 0
        assert p.top_k_overlap_min > 0
        assert p.mean_logprob_abs_tolerance > 0

    def test_registry_lookup_and_register(self):
        reg = CalibrationRegistry()
        prof = CalibrationRegistry().default()
        reg.register(prof)
        found = reg.lookup(
            model_family="unknown",
            gpu_class="default",
            engine="default",
            dtype="default",
            quantization="default",
        )
        assert found is not None
        assert found.profile_version == "stub-v0"

    def test_label_is_deterministic(self):
        p = CalibrationRegistry().default()
        assert p.label == "unknown/default/default/default/default"


# ---------------------------------------------------------------------------
# Verifier re-scorer + full-sequence comparison
# ---------------------------------------------------------------------------

class TestVerifierRescoring:
    def test_stub_rescore_is_deterministic_with_seed(self):
        stub = StubVerifierRescorer(model_family="stub", tolerance=0.0, seed=12345)
        out = stub.rescoring(
            prompt_token_ids=[1, 2],
            output_token_ids=[10, 11],
            sampling_params={"temperature": 0.7},
        )
        assert len(out) == 2
        assert out[0][0] == 10
        assert out[1][0] == 11

    def test_stub_matches_itself_within_tolerance(self):
        stub = StubVerifierRescorer(model_family="stub", tolerance=0.0, seed=99)
        output_ids = [10, 11, 12]
        verifier_records = stub.rescoring(
            prompt_token_ids=[1, 2, 3],
            output_token_ids=output_ids,
            sampling_params={},
        )
        # Build worker records that mirror the verifier output within tolerance
        worker_records = [
            TranscriptRecord(
                position=i,
                token_id=vid,
                chosen_logprob=vlogprob,
                top_k=tuple((tid, lp) for tid, lp in vtopk),
            )
            for i, (vid, vlogprob, vtopk) in enumerate(verifier_records)
        ]
        profile = CalibrationRegistry().default()
        challenged = [0]
        stats = compare_sequence(
            worker_records,
            verifier_records,
            profile=profile,
            challenged_positions=challenged,
        )
        assert stats.token_count == 3
        assert stats.chosen_all_within_tolerance
        assert stats.challenged_all_within_tolerance

    def test_comparison_rejects_mismatched_lengths(self):
        stub = StubVerifierRescorer(model_family="stub", tolerance=0.0, seed=1)
        worker = _simple_records(3)
        verifier = stub.rescoring(
            prompt_token_ids=[1, 2],
            output_token_ids=[10, 11],
            sampling_params={},
        )
        profile = CalibrationRegistry().default()
        assert len(worker) != len(verifier)
        with pytest.raises(ValueError, match="token counts must match"):
            compare_sequence(
                worker_records=worker,
                verifier_records=verifier,
                profile=profile,
                challenged_positions=[0],
            )


# ---------------------------------------------------------------------------
# Verdict types + signing + digest
# ---------------------------------------------------------------------------

class TestVerdictSigning:
    def test_verdict_types_are_the_three_expected(self):
        assert VerdictType.PASS == "pass"
        assert VerdictType.SUSPECT == "suspect"
        assert VerdictType.FAIL == "fail"

    def test_verdict_sign_and_verify_roundtrip(self):
        stats = VerificationStatistics(
            token_count=10,
            challenged_positions=[0, 1],
            comparisons=[],
            mean_abs_logprob_diff=0.0,
            max_abs_logprob_diff=0.0,
            tail_fraction=0.0,
            mean_top_k_overlap=1.0,
            chosen_all_within_tolerance=True,
            challenged_all_within_tolerance=True,
            output_hash_matches=True,
            token_count_matches=True,
            prompt_hash_matches=True,
        )
        verdict = Verdict(
            verdict_id="v-1",
            job_id="job-1",
            worker_node_id="node-1",
            verifier_id="verifier-1",
            type=VerdictType.PASS,
            transcript_root="root" + "0" * 59,
            output_hash="output" + "0" * 59,
            token_count=10,
            statistics=stats.as_dict(),
            tolerance_profile="unknown/default/default/default/default/stub-v0",
            profile_version="stub-v0",
            created_at=1760000000,
        )
        key = crypto.generate_ed25519()
        signed = verdict.sign(key["private_key"])
        assert signed["alg"] == "Ed25519"
        assert signed["domain"] == "tcdn-ai/verdict/v1"
        assert signed["key_id"]
        assert signed["sig"]
        reopened = Verdict.verify(signed, key["public_key"])
        assert reopened.verdict_id == "v-1"
        assert reopened.type == "pass"

    def test_statistics_digest_is_stable(self):
        stats = VerificationStatistics(
            token_count=4,
            challenged_positions=[0],
            comparisons=[],
            mean_abs_logprob_diff=0.01,
            max_abs_logprob_diff=0.02,
            tail_fraction=0.0,
            mean_top_k_overlap=0.8,
            chosen_all_within_tolerance=True,
            challenged_all_within_tolerance=True,
            output_hash_matches=True,
            token_count_matches=True,
            prompt_hash_matches=True,
        )
        a = statistics_digest(stats)
        b = statistics_digest(stats)
        assert a == b
        assert len(a) == 64


# ---------------------------------------------------------------------------
# VerdictRecord + penalty accounting
# ---------------------------------------------------------------------------

class TestVerdictRecordAndPenalties:
    def test_verdict_record_from_verdict(self):
        stats = VerificationStatistics(
            token_count=5,
            challenged_positions=[0],
            comparisons=[],
            mean_abs_logprob_diff=0.0,
            max_abs_logprob_diff=0.0,
            tail_fraction=0.0,
            mean_top_k_overlap=1.0,
            chosen_all_within_tolerance=True,
            challenged_all_within_tolerance=True,
            output_hash_matches=True,
            token_count_matches=True,
            prompt_hash_matches=True,
        )
        verdict = Verdict(
            verdict_id="v-2",
            job_id="job-2",
            worker_node_id="node-2",
            verifier_id="verifier-2",
            type=VerdictType.PASS,
            transcript_root="r" + "0" * 63,
            output_hash="o" + "0" * 63,
            token_count=5,
            statistics=stats.as_dict(),
            tolerance_profile="p",
            profile_version="v0",
            created_at=1760000001,
            appeal_id=None,
        )
        from app.ai.verification2 import VerdictRecord
        rec = VerdictRecord.from_verdict(
            verdict,
            statistics_digest=statistics_digest(stats),
            penalty_applied=False,
        )
        assert rec.verdict_id == "v-2"
        assert rec.statistics_digest == statistics_digest(stats)
        assert rec.penalty_applied is False

    def test_penalty_accounts_for_destiny_are_flagged_places(self):
        debit, credit = penalty_accounts_for_destiny("client_refund")
        assert "__VOID__" in (debit, credit)
        assert "__CLIENT_REFUND__" in (debit, credit)

        _, credit = penalty_accounts_for_destiny("insurance_pool")
        assert credit == "pool:insurance"

        _, credit = penalty_accounts_for_destiny("platform")
        assert credit == "platform:penalty-revenue"

        with pytest.raises(ValueError, match="unknown penalty destiny"):
            penalty_accounts_for_destiny("bogus")

    def test_penalty_record_shape(self):
        rec = PenaltyRecord(
            penalty_id="p-1",
            job_id="job-3",
            node_id="node-3",
            actor="verifier-3",
            kind="void_job_earnings",
            amount_micro=5000,
            debit_account="__VOID__",
            credit_account="__CLIENT_REFUND__",
            reason="deterministic violation",
            created_at=1760000002,
            idempotent_key="job-3:verifier-3:void_job_earnings",
        )
        assert rec.amount_micro == 5000
        assert rec.reversible is True


# ---------------------------------------------------------------------------
# Privacy canary helper
# ---------------------------------------------------------------------------

class TestPrivacyCanary:
    CANARY = "TCDN_AI_PRIVACY_CANARY"

    def test_canary_detected_in_str(self):
        assert has_canary(self.CANARY, self.CANARY) is True

    def test_canary_detected_nested(self):
        structure = {"outer": ["keep", {"inner": self.CANARY}]}
        assert has_canary(structure, self.CANARY) is True

    def test_canary_not_present(self):
        assert has_canary({"prompt": "hello world"}, self.CANARY) is False


# ---------------------------------------------------------------------------
# Verifier signing layer: verifier_sign::ed25519_sign + VerificationVoucher
# ---------------------------------------------------------------------------

class TestVerifierSignSurface:
    def test_ed25519_sign_round_trips_and_binds_domain_key_and_payload(self):
        payload = {"job_id": "job-sign", "created_at": int(time.time())}
        voucher = ed25519_sign(
            VerificationVoucherDomain.VERIFIER_VERDICT,
            payload,
            key=_TEST_PRIVATE_B64,
        )
        assert isinstance(voucher, VerificationVoucher)
        assert voucher.domain == VerificationVoucherDomain.VERIFIER_VERDICT
        assert voucher.key_id == crypto.key_id_for(_TEST_PUBLIC_B64)
        assert voucher.alg == "Ed25519"
        assert voucher.payload == payload

        verified = verify_voucher(voucher, public_key_b64=_TEST_PUBLIC_B64)
        assert verified.payload == payload

        # Payload tampering must invalidate the signature.
        tampered = VerificationVoucher(
            domain=voucher.domain,
            payload={**payload, "job_id": "other-job"},
            key_id=voucher.key_id,
            alg=voucher.alg,
            sig=voucher.sig,
        )
        with pytest.raises(VoucherVerificationError, match="signature"):
            verify_voucher(tampered, public_key_b64=_TEST_PUBLIC_B64)

        # A valid signature must not be relabelled to another key.
        other_key = crypto.generate_ed25519()
        with pytest.raises(VoucherVerificationError, match="key_id"):
            verify_voucher(voucher, public_key_b64=other_key["public_key"])

        # Domains are a closed set; neither signing nor verification accepts an
        # arbitrary string that could alias a ticket or receipt domain.
        with pytest.raises(VoucherVerificationError, match="domain"):
            verify_voucher(
                VerificationVoucher(
                    domain="tcdn-ai/ticket/v1",
                    payload=payload,
                    key_id=voucher.key_id,
                    alg="Ed25519",
                    sig=voucher.sig,
                ),
                public_key_b64=_TEST_PUBLIC_B64,
            )
        with pytest.raises(ValueError, match="unsupported"):
            ed25519_sign("tcdn-ai/ticket/v1", payload, key=_TEST_PRIVATE_B64)

    def test_sign_challenge_response_produces_a_verifiable_voucher(self):
        secret = _deterministic_secret()
        rng = VerificationRng(secret)
        records = _simple_records(9)
        root = build_transcript_root(records)
        indices = rng.challenge_indices("job-challenge", root, token_count=9)
        opened = [records[i].to_challenge_response() for i in indices]

        voucher = sign_challenge_response(
            job_id="job-challenge",
            node_public_key_b64=_TEST_PUBLIC_B64,
            transcript_root=root,
            challenged_positions=indices,
            opened_records=opened,
            private_key_b64=_TEST_PRIVATE_B64,
        )
        assert isinstance(voucher, VerificationVoucher)
        assert voucher.domain == VerificationVoucherDomain.CHALLENGE_RESPONSE
        verified = verify_voucher(voucher, public_key_b64=_TEST_PUBLIC_B64)
        assert verified.payload["transcript_root"] == root
        assert verified.payload["challenged_positions"] == indices
        assert verified.payload["opened_records"] == opened
        assert verified.payload["job_id"] == "job-challenge"

    def test_sign_verifier_verdict_build_is_real_and_sealed_by_seam(self):
        stats = VerificationStatistics(
            token_count=7,
            challenged_positions=[0],
            comparisons=[],
            mean_abs_logprob_diff=0.0,
            max_abs_logprob_diff=0.0,
            tail_fraction=0.0,
            mean_top_k_overlap=1.0,
            chosen_all_within_tolerance=True,
            challenged_all_within_tolerance=True,
            output_hash_matches=True,
            token_count_matches=True,
            prompt_hash_matches=True,
        )
        voucher = sign_verifier_verdict(
            verdict_id="v-signed",
            job_id="job-verdict",
            worker_node_id="node-signed",
            verifier_id="verifier-signed",
            verdict_type=VerdictType.FAIL,
            transcript_root="r" + "0" * 63,
            output_hash="o" + "0" * 63,
            token_count=7,
            statistics=stats.as_dict(),
            tolerance_profile="p",
            profile_version="v0",
            created_at=1760000003,
            appeal_id=None,
            private_key_b64=_TEST_PRIVATE_B64,
        )
        assert isinstance(voucher, VerificationVoucher)
        assert voucher.domain == VerificationVoucherDomain.VERIFIER_VERDICT
        verified = verify_voucher(voucher, public_key_b64=_TEST_PUBLIC_B64)
        assert verified.payload["verdict_id"] == "v-signed"
        assert verified.payload["type"] == VerdictType.FAIL
        assert len(verified.sig) > 16

    def test_voucher_payload_bytes_are_deterministic(self):
        v = VerificationVoucher(
            domain=VerificationVoucherDomain.VERIFIER_VERDICT,
            payload={"a": 1, "b": 2},
            key_id="kid",
            alg="Ed25519",
            sig="sig",
        )
        assert v.payload_bytes() == b'{"a":1,"b":2}'

    def test_ed25519_sign_validates_domain_enum_not_invented_by_caller(self):
        # The spec wants the signing domain to be explicit and disjoint from
        # receipt/ticket domains. The enum is the only accepted source, and the
        # runtime check rejects a free-form string too.
        from app.ai.verifier_sign import VerificationVoucherDomain as V

        assert V.CHALLENGE_RESPONSE.startswith("tcdn-ai/verification2/")
        assert V.VERIFIER_VERDICT.startswith("tcdn-ai/verification2/")
        assert V.VERIFIER_RECEIPT.startswith("tcdn-ai/verification2/")
        assert V.CHALLENGE_RESPONSE != V.VERIFIER_VERDICT
        assert V.VERIFIER_VERDICT != V.VERIFIER_RECEIPT


# ---------------------------------------------------------------------------
# Worker capture + encrypted retention (milestone 1)
# ---------------------------------------------------------------------------


class TestTranscriptRecorder:
    def test_records_are_contiguous_and_root_matches_commitment(self):
        recorder = TranscriptRecorder()
        for index in range(6):
            recorder.record(
                token_id=100 + index,
                chosen_logprob=-0.5 - index * 0.01,
                top_k=((100 + index, -0.5), (55, -1.5)),
            )
        assert [r.position for r in recorder.records] == list(range(6))
        assert recorder.build_root() == build_transcript_root(list(recorder.records))

    def test_chosen_token_is_always_in_top_k_and_k_is_bounded(self):
        recorder = TranscriptRecorder(top_k=3)
        record = recorder.record(
            token_id=7,
            chosen_logprob=-1.0,
            top_k=((1, -2.0), (2, -2.1), (3, -2.2), (4, -2.3)),
        )
        ids = [token_id for token_id, _ in record.top_k]
        assert ids[0] == 7
        assert len(record.top_k) == 3

    def test_invalid_logprobs_are_rejected(self):
        recorder = TranscriptRecorder()
        with pytest.raises(ValueError, match="finite"):
            recorder.record(token_id=1, chosen_logprob=float("nan"))
        with pytest.raises(ValueError, match="positive"):
            recorder.record(token_id=1, chosen_logprob=0.5)
        with pytest.raises(ValueError, match="token_id"):
            recorder.record(token_id=-1, chosen_logprob=-0.5)

    def test_vllm_logprob_content_is_captured(self):
        recorder = TranscriptRecorder()
        recorder.extend_from_logprobs(
            [
                {
                    "token_id": 42,
                    "logprob": -0.25,
                    "top_logprobs": [
                        {"token_id": 42, "logprob": -0.25},
                        {"token_id": 43, "logprob": -1.75},
                    ],
                },
                {"token_id": 44, "logprob": -0.5, "top_logprobs": []},
            ]
        )
        assert [r.token_id for r in recorder.records] == [42, 44]
        assert recorder.records[0].position == 0
        assert recorder.records[1].position == 1

    def test_partial_engine_logprobs_are_refused(self):
        recorder = TranscriptRecorder()
        with pytest.raises(ValueError, match="token_id"):
            recorder.extend_from_logprobs([{"logprob": -0.5, "top_logprobs": []}])


class TestEncryptedTranscriptStore:
    def _clock(self):
        now = [1_000]

        def clock():
            return now[0]

        return now, clock

    def test_round_trip_and_ciphertext_at_rest(self):
        key = crypto.generate_ed25519()
        _now, clock = self._clock()
        store = EncryptedTranscriptStore(key["private_key"], ttl_seconds=60, clock=clock)
        records = _simple_records(4)
        store.put("job-1", records)

        blob, expires_at = store._items["job-1"]
        assert expires_at == 1_060
        assert b"job-1" not in blob
        # The stored bytes are the AEAD ciphertext, not the JSON document.
        assert b'"records"' not in blob
        assert b'"lp"' not in blob

        reopened = store.get("job-1")
        assert [(r.position, r.token_id) for r in reopened] == [
            (r.position, r.token_id) for r in records
        ]
        assert reopened[1].chosen_logprob == records[1].chosen_logprob

    def test_expiry_wipes_the_records(self):
        key = crypto.generate_ed25519()
        now, clock = self._clock()
        store = EncryptedTranscriptStore(key["private_key"], ttl_seconds=10, clock=clock)
        store.put("job-1", _simple_records(2))
        now[0] += 10
        with pytest.raises(KeyError):
            store.get("job-1")
        assert len(store) == 0

    def test_wipe_and_wrong_key(self):
        key = crypto.generate_ed25519()
        other = crypto.generate_ed25519()
        _now, clock = self._clock()
        store = EncryptedTranscriptStore(key["private_key"], ttl_seconds=60, clock=clock)
        store.put("job-1", _simple_records(2))
        store.wipe("job-1")
        with pytest.raises(KeyError):
            store.get("job-1")

        store.put("job-2", _simple_records(2))
        attacker = EncryptedTranscriptStore(other["private_key"], ttl_seconds=60, clock=clock)
        attacker._items = store._items
        with pytest.raises(crypto.CryptoError):
            attacker.get("job-2")

    def test_empty_transcript_is_not_stored(self):
        key = crypto.generate_ed25519()
        store = EncryptedTranscriptStore(key["private_key"])
        with pytest.raises(ValueError):
            store.put("job-1", [])
