# Key inventory and secret-store mode (checklist 5.2)

Every long-lived secret the TCDN-AI module holds, where it lives, how it is
rotated, and the test that proves the property. Citations are resolved by
`tests/test_ai_crypto_review_docs.py`, so a renamed symbol or deleted test fails
the suite instead of leaving a stale inventory.

## 1. Keys

| Key | Algorithm / size | Who holds the private half | Stored where | Lifetime | Rotation | Revocation | Pinned by |
|---|---|---|---|---|---|---|---|
| Coordinator signing key | Ed25519 | Coordinator only | Secret store: 0600 key file, or the keyring JSON injected by the platform (read-only) | Indefinite; rotated by an operator | `KeyRing.rotate` keeps the old key verifying for `key_overlap_seconds`, then it stops signing and stops verifying after the overlap | `KeyRing.revoke` puts the key id in the signed revocation list; the last signing key cannot be revoked | `TestTickets::test_rotation_overlap_then_expiry`, `TestTickets::test_revoked_key_is_rejected` |
| Coordinator revocation list | Ed25519-signed document | Coordinator | Served at `/api/ai/v1/keys/coordinator` | Same as the signing key | Automatic on every `revoke` | — | `TestTickets::test_cannot_revoke_last_signing_key` |
| API-key pepper | 256-bit secret | Coordinator only | `TCDN_AI_API_KEY_PEPPER` env / platform secret store | Indefinite | Changing it invalidates every API key (documented, deliberate) | — | `TestProductionGates::test_a_complete_production_config_is_accepted` |
| Verification sampling key | 256-bit secret (HMAC) | Coordinator only | `TCDN_AI_VERIFICATION_SAMPLE_KEY`, falling back to the API-key pepper | Indefinite | Changing it changes which future jobs are sampled; past samples are unaffected | — | `TestVerification::test_the_shipping_rate_is_three_percent_and_needs_a_secret` |
| Payment webhook secret | Paystack secret key (HMAC-SHA-512) | Coordinator only | `TCDN_AI_PAYSTACK_SECRET_KEY` env | Provider-managed | Rotate in the Paystack dashboard, then the env | Old secret stops verifying immediately | `TestPaystackProvider::test_wrong_signature_rejected` |
| Worker identity key | Ed25519 | The worker operator | Worker's own storage; only the public key reaches the coordinator | Indefinite | New key = new `node_id` (the id is derived from the public key) | Remove the node; its signatures no longer resolve | `TestCrypto::test_sign_verify_and_tamper` |
| Worker session key | X25519 | The worker | Worker memory, bound into the attestation verdict | Rotated per session | Re-attested session key replaces the old binding | `key_binding_mismatch` on a stale binding | `TestAttestation::test_nonce_is_bound_to_node` |
| Per-job frame keys | X25519 → HKDF-SHA-256 → AES-256-GCM | Client (SDK) and worker | Derived in memory per job, never persisted | The job | New ephemeral key per job | Job end | `TestCrypto::test_hkdf_is_deterministic_and_direction_separated` |
| Host session JWT secret (product) | HMAC, 256-bit | CDN backend only | `JWT_SECRET` env | Indefinite | Rotating it logs everyone out | `security_stamp` / `pw_change` markers revoke individual sessions | `TestAccessTokenClaimRequirements::test_bare_sub_token_is_rejected` |

## 2. Secret-store mode in production

`load_keyring` accepts exactly two sources and refuses everything else:

* **Injected JSON** — `TCDN_AI_COORDINATOR_KEYRING_JSON`, read through
  `EnvSecretStore`. Read-only by design: `EnvSecretStore.save` raises, because a
  secret manager is the system of record and the process must never write back.
  This is the mode a managed platform uses (Render secret file / env var).
* **Key file** — `TCDN_AI_COORDINATOR_KEY_FILE`, read through
  `FileSecretStore`, which refuses a file that is group- or world-readable
  (mode must be 0600). This is the mode a self-hosted deployment uses.

Production validation (`AISettings`) refuses to start when neither source is
present (`TCDN_AI_COORDINATOR_KEY_FILE or TCDN_AI_COORDINATOR_KEYRING_JSON must
supply the coordinator keyring in production`), so a production process cannot
silently fall back to an ephemeral in-memory key — and an ephemeral key would
make every issued ticket unverifiable after a restart.

No key material is tracked in git: the inventory ships, the keys do not. The
repository contains documentation and `.example` files only, and the secret scan
(row 1.5) is the mechanical check for that.

## 3. Rotation with overlap

`KeyRing.rotate` adds a new active key; the previous one stays in
`can_verify` for `key_overlap_seconds` (`TCDN_AI_KEY_OVERLAP_SECONDS`, default
3600). That window exists because a worker may be holding a ticket signed
seconds before the rotation: without overlap, every in-flight job would fail
verification at the worker with no way to distinguish rotation from an attack.

The overlap is bounded and tested (`test_rotation_overlap_then_expiry`): after
the window the old key neither signs nor verifies, and the public bundle stops
advertising it.

## 4. What is not a key (and must never become one)

* The verification sample is keyed (see `CRYPTO_REVIEW.md`) but the key never
  leaves the coordinator; workers never receive it, so they cannot test a
  candidate secret offline against a job id.
* Node identity is derived (`node_` + SHA-256 of the public key), not assigned,
  so a node cannot claim another's id without its private key.
* No shared symmetric secret exists between workers and the coordinator: every
  worker request is signed per message with the worker's Ed25519 key.

## 5. Open gaps

1. Key rotation is an operator action (`scripts/ai_provision.py rotate`); there
   is no scheduled rotation and no automatic expiry of the signing key.
2. `EnvSecretStore` cannot rewrite the injected JSON, so rotating the keyring in
   a managed platform requires updating the platform secret and restarting.
3. Worker key history is not audited beyond the node record; an operator that
   re-registers a node creates a new node id rather than a key history.
