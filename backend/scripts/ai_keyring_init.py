#!/usr/bin/env python3
"""Print a fresh coordinator keyring, ready to paste into a secret store.

The coordinator signs ticket documents with an Ed25519 keyring. In production
the keyring is *injected* (``TCDN_AI_COORDINATOR_KEYRING_JSON``) and is never
written back, so it has to be created once, out of band, and stored — Render's
env var, a KMS, systemd credentials. This prints exactly that value.

    python scripts/ai_keyring_init.py --name coordinator-staging

Paste the single line it prints into the env var. Keep it somewhere safe: it is
the only copy of the private key, and rotating it means publishing a new public
bundle (see RUNBOOK.md "Key rotation"). Output goes to stdout only — never log
it, never commit it, and remember that a terminal scrollback is a secret store
of the worst kind.

``--check`` verifies an existing JSON without printing key material, which is
what a deploy pipeline should run against its own secret.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ai.keyring import KeyRing


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default="coordinator-staging")
    parser.add_argument(
        "--check",
        metavar="JSON_OR_ENV_VAR",
        help="validate a keyring JSON (or the env var holding it) without printing it",
    )
    args = parser.parse_args()

    if args.check:
        raw = args.check
        if not raw.lstrip().startswith("{"):
            import os

            raw = os.environ.get(raw, "")
            if not raw:
                raise SystemExit(f"{args.check} is not set")
        try:
            ring = KeyRing.from_secret(json.loads(raw))
        except Exception as exc:  # reported as a failed check, not a traceback
            raise SystemExit(f"keyring is not usable: {exc}") from exc
        active = ring.active
        print(
            f"keyring ok: name={ring.name} keys={len(ring.to_secret()['keys'])} "
            f"active={active.key_id} can_sign={active.can_sign}"
        )
        return

    ring = KeyRing.generate(args.name)
    # One line, no indentation: an env var that wraps is a support ticket.
    print(json.dumps(ring.to_secret(), separators=(",", ":")))


if __name__ == "__main__":
    main()
