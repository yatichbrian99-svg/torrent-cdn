# Cryptographic review (checklist 5.1)

Status: **self-review, internal**. This is the artifact row 5.1 asks for — every
primitive, where it is used, why it is a vetted library call rather than
hand-written code, and the test that pins the property. It is *not* an
independent review and does not satisfy 5.14 (third-party pentest + crypto
review), which remains an owner action.

## 1. Method

1. Enumerate every import of `cryptography`, `hashlib`, `hmac` and `secrets` in
   `backend/app/ai` and `backend/tcdn_ai_sdk`.
2. For each, record the algorithm, the exact call, the property the system
   depends on, and the test that fails if the property is lost.
3. Record what is deliberately **not** used, because "we do not roll our own
   cipher" is a claim about absence.

Every citation below is resolved mechanically by
`tests/test_ai_crypto_review_docs.py`: a renamed function or deleted test fails
the suite rather than leaving a stale document.

## 2. Primitive inventory

| Purpose | Primitive | Call site | Property relied on | Pinned by |
|---|---|---|---|---|
| Request/ticket/receipt signatures | Ed25519 (RFC 8032) | `app/ai/crypto.py::sign`, `app/ai/crypto.py::verify` | EUF-CMA under the node or coordinator key; signatures are deterministic and 64 bytes | `TestCrypto::test_sign_verify_and_tamper` |
| Wrong-key rejection | Ed25519 | `app/ai/crypto.py::verify` | A signature from another key never verifies | `TestCrypto::test_verify_with_wrong_key_is_rejected` |
| Domain separation | SHA-256 over canonical JSON with an explicit domain | `app/ai/crypto.py::domain_message` | A signature for one document type cannot be replayed as another | `TestTickets::test_cross_domain_signature_is_rejected` |
| Canonical serialisation | Sorted-key UTF-8 JSON | `app/ai/crypto.py::canonical_json` | The bytes signed are the bytes verified, independent of dict order | `TestCrypto::test_sign_verify_and_tamper` |
| Key agreement | X25519 (RFC 7748) | `app/ai/crypto.py::x25519_shared_secret` | An ephemeral shared secret only the ticket holder can derive | `TestCrypto::test_x25519_rejects_degenerate_peer` |
| Key derivation | HKDF-SHA-256 with salt + info | `app/ai/crypto.py::hkdf_sha256`, `app/ai/crypto.py::derive_session_keys` | Independent keys per direction; salt and info are mandatory | `TestCrypto::test_hkdf_is_deterministic_and_direction_separated` |
| Payload encryption | AES-256-GCM | `app/ai/crypto.py::aead_encrypt`, `app/ai/crypto.py::aead_decrypt` | Confidentiality + integrity; AAD binds the frame header | `TestCrypto::test_aead_roundtrip_and_tamper` |
| Nonce uniqueness | 96-bit random nonce per message | `app/ai/crypto.py::aead_encrypt` | A nonce is never reused under one key, so GCM's catastrophic failure mode is unreachable | `TestAttestation::test_nonce_cannot_be_reused` |
| Attestation freshness | Single-use nonce with expiry | `app/ai/attestation.py::AttestationService.issue_nonce` | Replaying an attestation report fails the second time | `TestAttestation::test_nonce_is_bound_to_node` |
| API-key storage | SHA-256 + HMAC-SHA-256 pepper | `app/ai/crypto.py::hash_api_key` | A database leak exposes neither the key nor a cheap offline target | `TestCrypto::test_api_key_hashing_uses_pepper` |
| Key identifiers | SHA-256 of the public key, truncated | `app/ai/crypto.py::key_id_for` | Key selection is unambiguous without trusting the caller's label | `TestTickets::test_unknown_key_id_is_rejected` |
| Constant-time compare | `hmac.compare_digest` | `app/ai/crypto.py::constant_time_eq` | No timing oracle on signatures, key ids, hashes or webhook MACs | `TestCrypto::test_sign_verify_and_tamper` |
| Webhook authentication | HMAC-SHA-512 over the raw body | `app/ai/payments.py::PaystackPaymentProvider.verify_webhook` | A forged payment callback cannot be minted without the provider secret | `TestPaystackProvider::test_wrong_signature_rejected` |
| Redundant-execution sampling | HMAC-SHA-256, coordinator key | `app/ai/verification.py::VerificationSampler.decide` | A worker holding the job id cannot predict whether it will be checked | `TestVerification::test_sample_is_not_derivable_from_the_job_id` |
| Content hashing | SHA-256 | `app/ai/crypto.py::sha256_hex` | Receipts, Merkle leaves and shard digests are collision-resistant | `TestTranscriptCommitment::test_build_root_and_proof` |
| Memory hygiene | `bytearray` zeroing | `app/ai/crypto.py::zeroise` | Key material is not left in a long-lived buffer | `TestCrypto::test_zeroise_clears_buffer` |
| Randomness | `os.urandom` / `secrets` only | `app/ai/crypto.py::generate_ed25519`, `app/ai/crypto.py::new_session_nonce` | Keys and nonces come from the OS CSPRNG | `TestCrypto::test_api_key_hashing_uses_pepper` |

## 3. Domain separation

Every signed object carries a literal domain string in the message that is
signed, so a signature over one shape cannot be presented as another:

`tcdn-ai/node-register/v1`, `tcdn-ai/heartbeat/v1`,
`tcdn-ai/attestation-evidence/v1`, `tcdn-ai/ticket/v1`,
`tcdn-ai/receipt/v1`, `tcdn-ai/transfer-receipt/v1`,
`tcdn-ai/verification/sample/v1`, `tcdn-ai/verification/sample-key/v1`,
`tcdn-ai/challenge/v1`, `tcdn-ai/verdict/v1`.

The receipt domain, for instance, is fixed by the wire format and never taken
from the request (`Coordinator.submit_receipt`), so a ticket signature cannot be
submitted as a receipt (`TestTickets::test_cross_domain_signature_is_rejected`,
`TestTickets::test_cross_domain_signature_is_rejected`).

## 4. Nonce and AAD discipline

* **AES-GCM nonces** are 12 random bytes per message. The key is derived per job
  from an ephemeral X25519 exchange, so the nonce space is per-job as well; the
  same key is never used across two jobs.
* **AAD** is the frame header (direction, sequence, job id), so a reordered or
  relabelled frame fails authentication rather than decrypting to the wrong
  plaintext (`TestCrypto::test_aead_roundtrip_and_tamper`).
* **Attestation nonces** are single-use, node-bound and expiring; the Redis
  store enforces single use across coordinator instances
  (`TestSharedStateAcrossInstances::test_a_nonce_issued_by_one_instance_is_accepted_by_the_other`).
* **Ticket `jti`** is claimed once in the replay cache, Redis-backed in
  production (`TestSharedStateAcrossInstances::test_a_ticket_claimed_by_one_instance_is_used_up`).

## 5. Deliberately absent

* No custom cipher, mode, padding or KDF is implemented in this repository; the
  only arithmetic is the Merkle tree, which is a hash construction, and it is
  tested against a manual reference (`TestTranscriptCommitment::test_build_root_and_proof`).
* No MD5, SHA-1, ECB, static IV or non-AEAD mode appears in `app/ai` or
  `tcdn_ai_sdk` (the static check in `TestStaticSafety` scans the package).
* No key material is committed: key files are 0600 and refused otherwise
  (`TestSecretHandling::test_keyring_file_must_be_0600`), and production must
  inject the keyring (`TestProductionGates`).

## 6. Open gaps

1. No independent review (5.14) and no formal verification; this document is a
   structured self-review.
2. TLS is terminated by the deployment (Caddy/most managed hosts). The package
   requires TLS in production (`require_tls`), but cipher-suite policy belongs
   to that layer and is not tested here.
3. Key material lives in process memory for the process lifetime; `zeroise` is
   used on intermediate buffers, not on long-lived key objects, because the
   `cryptography` library does not expose their internals.
4. Side-channel resistance of the Merkle/verifier code is inherited from the
   library primitives; the comparisons we wrote use `hmac.compare_digest`, but
   there is no timing analysis of the surrounding control flow.
