# Confidential tier — hardware and platform notes

Status: **the confidential tier is exercisable only with the simulated verifier in
this build.** No real attestation report parser, vendor roots, or CoCo deployment
manifest is shipped. Do not present a deployment based on this file as
confidential until the verifier and the deployment are independently validated.

## What the tier requires

1. **GPU confidential computing.** NVIDIA H100/H200/Blackwell-class hardware in CC
   mode, with the driver and firmware capable of producing an attestation report
   that includes GPU measurements and the CC-mode flag. Consumer GPUs do not
   qualify.
2. **CPU-side confidential VM.** AMD SEV-SNP or Intel TDX, verified by remote
   attestation against the vendor's root of trust with a current TCB level.
3. **Confidential Containers.** Kata Containers microVM with encrypted guest
   memory, no host access to guest memory, and a measured guest image. Kubernetes
   with the CoCo operator (or an equivalent) is the usual path.
4. **Key binding.** The per-container Ed25519 key and the worker X25519 session key
   must be included in the attestation report data (as hashes). The coordinator
   rejects a verdict whose `session_key_sha256` does not equal the registered
   session key hash — this is enforced in code and tested.
5. **Freshness.** The coordinator issues a single-use nonce
   (`POST /nodes/{id}/attestation-nonce`); evidence older than
   `TCDN_AI_ATTESTATION_NONCE_TTL_SECONDS` is rejected.
6. **No SGX.** Intel SGX enclave memory is far too small for LLM inference and does
   not cover GPU VRAM. It is not an acceptable substitute.

## Testing on a VPS

Nested virtualisation can run Kata/CoCo functionally (guest boots, container runs,
attestation *plumbing* works). A VPS with nested virt **does not** provide real
confidentiality: the cloud provider can read the host. Use it to validate the
control plane only, and label it `attestation: simulated` in any report.

## What is implemented here

| Piece | State |
|---|---|
| `AttestationVerifier` interface + policy (`allowed_platforms`, measurements, TCB floor, vendor roots) | Implemented |
| Nonce issuance/consumption, key binding, verdict expiry | Implemented and tested |
| Coordinator-signed verdict document | Implemented |
| `SimulatedVerifier` | Implemented, labelled, refused in production |
| `SevSnpVerifier` / `TdxVerifier` / `NvidiaGpuVerifier` | Fail closed with `attestation_verifier_unavailable`; report parsing not shipped |
| CoCo/Kata manifests | Not shipped (this note only) |
| Scheduler refusal to downgrade | Implemented and tested |
| Worker refusal without attestation | Implemented and tested |

## Next steps before any real confidential claim

1. Integrate a vendor verifier (AMD KDS, Intel PCS/PCS-backed verifier, NVIDIA
   NRAS) behind the existing interface.
2. Publish the exact measurement allowlist and minimum TCB per image.
3. Deploy under CoCo/Kata with an independently reviewed manifest.
4. Have the isolation claim reviewed by someone who does not work on this codebase.
5. Keep `allow_simulated_attestation=false` in production (already enforced).
