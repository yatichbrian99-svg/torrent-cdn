# TCDN-AI — open questions

These are follow-up clarification requests attached to the config intake for this
session. They are listed here because the fields in question were mentioned as
intake topics but are **not present in the repo's `.env.example`** or in the
existing TCDN-AI docs. Each question is independent; resolving one does not block
the others.

## Q1 — HPA fields

**Question:** What HPA fields are you expecting to configure for TCDN-AI, and
where should they live?

**What I found:** The repo has no `HPA_*` variables, no `HORIZONTAL_POD_AUTOSCALER_*`
variables, and no HPA-related documentation in `docs/tcdn-ai/`. The deployment
sketch in `deploy/tcdn-ai/docker-compose.dev.yml` is a manual coordinator + worker
+ engine compose, not a Kubernetes manifest, so there is no HPA object to point to.

**Why it matters:** If "HPA" here means Kubernetes horizontal pod autoscaling for
the coordinator and/or workers, the config intake needs the exact env or ConfigMap
fields you want mapped (e.g. min/max replicas, target CPU/memory, scaling behavior),
and whether they should be documented in a new deploy guide or in `CONFIG_INTAKE_SUMMARY.md`
once the deployment target is confirmed.

**Ask:** Confirm the intended platform (Kubernetes, Docker Compose, Render, other)
and the exact HPA fields you want included in the config intake. If HPA is not
relevant here, confirm that so I can drop it from the intake record rather than
leave it as an open question.

## Q2 — DPS `redis_delimiter` env

**Question:** What is the `DPS` module, and which environment variable is the
`redis_delimiter`? Where should it be documented?

**What I found:** The repo has no `DPS_*` variables, no `REDIS_DELIMITER` variable,
and no reference to a "DPS" component in the TCDN-AI docs. Redis configuration in
this repo is the standard block (`REDIS_URL`, `DB_POOL_*`, `TRUSTED_PROXIES`, etc.),
and there is no delimiter-related Redis setting in `.env.example`.

**Why it matters:** If `redis_delimiter` is a real config value for a dependency
or an internal DPS service that talks to Redis, I need the env var name, its type,
its default, and whether it is required. Without that, the intake table cannot map
it and the README/runbook cannot document it accurately.

**Ask:** Point me at the source of the `DPS` / `redis_delimiter` requirement — a
config file, a deploy guide, a third-party service, or a desired internal feature —
and the exact env var name. If it is not part of this project, confirm that and I
will record it as out-of-scope rather than leaving it ambiguous.

## Q3 — storage `type` / `value` debug log

**Question:** What is the storage `type`/`value` debug log you want surfaced, and
through which env variable?

**What I found:** The repo's storage configuration is the S3/MinIO block in
`.env.example` (`MINIO_ENDPOINT`, `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY`,
`MINIO_BUCKET`, `MINIO_SECURE`, `MINIO_PUBLIC_URL`, `MINIO_REGION`). There is no
`storage.type`, `storage.value`, or storage-specific debug-log variable in
`.env.example`, and no storage debug-log mention in `docs/tcdn-ai/`.

**Why it matters:** If you want a debug log that prints the storage backend type
and its configured value at startup (or per upload), the intake needs the exact
env var name(s), the intended log location/verbosity, and whether it should be
feature-flagged or only wired for development. Without that, I cannot add a
meaningful config entry without inventing a field.

**Ask:** Confirm the intended env var name(s), the verbosity/scope of the debug
log, and whether it is a new feature you want added or an existing setting I
should be documenting from somewhere else in the repo.

## How to resolve

Reply with the missing context for whichever questions apply. I will then either:

- map the field into `CONFIG_INTAKE_SUMMARY.md` if it belongs to this project, or
- record it as out-of-scope with a one-line reason if it does not.

Until then, these three items stay open and are referenced from
`CONFIG_INTAKE_SUMMARY.md`.
