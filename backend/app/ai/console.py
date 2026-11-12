"""Data assembly for the TCDN-AI developer console.

The console is a *browser* client, so it cannot use the API-key surface the
gateway exposes (``/balance`` and ``/usage`` need a key that the console is
itself there to create). This module turns store records into the small,
deliberately boring JSON the console renders. It holds no routes and no auth;
see ``routes_console.py`` for those.

Rules this file keeps:

* prompts and outputs are not stored anywhere, so nothing here can leak them —
  job rows carry counts, prices and status only;
* node identities stay server-side: the console gets a region name, never a node
  id, an endpoint or a key id that belongs to anyone but the caller;
* prices come from the catalogue the coordinator already serves, so the console
  cannot show a number the gateway would not charge.
"""

from __future__ import annotations

from typing import Any

from .models import ModelInfo
from .pricing import MICRO_PER_CREDIT
from .store import ApiKeyRecord, JobRecord, NodeRecord

#: KES per USD used for the "≈ KES" line. It is a display rate for the packs
#: below, not an FX quote: the price of a pack is fixed in KES, and the USD
#: credit it buys is fixed too. Change both together or the UI lies.
KES_PER_USD = 130

#: Purchasable packs. ``kes_minor`` is KES cents (Paystack's smallest unit);
#: ``credits_usd`` is what lands in the ledger (1 credit = 1 USD = 1e6 micro).
PACKS: tuple[dict[str, Any], ...] = (
    {"id": "starter", "label": "Starter", "credits_usd": 5, "kes_minor": 65000},
    {"id": "builder", "label": "Builder", "credits_usd": 20, "kes_minor": 260000},
    {"id": "studio", "label": "Studio", "credits_usd": 50, "kes_minor": 650000},
    {"id": "scale", "label": "Scale", "credits_usd": 100, "kes_minor": 1300000},
)

_SCORPES = ("inference", "worker")


def packs() -> list[dict[str, Any]]:
    """The pack list as the console needs it: USD credits and a KES price."""
    return [
        {
            "id": pack["id"],
            "label": pack["label"],
            "credits_usd": pack["credits_usd"],
            "credit_micro": int(pack["credits_usd"]) * MICRO_PER_CREDIT,
            "kes": pack["kes_minor"] / 100,
            "kes_minor": pack["kes_minor"],
            "currency": "KES",
        }
        for pack in PACKS
    ]


def pack(pack_id: str) -> dict[str, Any] | None:
    for entry in packs():
        if entry["id"] == pack_id:
            return entry
    return None


def valid_scopes(scopes: list[str] | None) -> tuple[str, ...]:
    """Reject anything the gateway would not honour anyway."""
    requested = tuple(
        scope.strip() for scope in (scopes or []) if scope and scope.strip()
    )
    if not requested:
        return ("inference",)
    unknown = sorted(set(requested) - set(_SCORPES))
    if unknown:
        raise ValueError(f"unknown scope(s): {', '.join(unknown)}")
    # Order-stable so a repeated request produces an identical list.
    return tuple(scope for scope in _SCORPES if scope in requested)


def key_view(record: ApiKeyRecord) -> dict[str, Any]:
    """Everything the console may know about a key.

    ``record.key_hash`` is deliberately absent: the browser has no use for it,
    and an XSS or a screenshot cannot leak what was never sent.
    """
    return {
        "id": record.key_id,
        "prefix": record.prefix,
        "scopes": list(record.scopes),
        "status": record.status,
        "created_at": record.created_at,
        "last_used_at": record.last_used_at,
        "revoked_at": record.revoked_at,
    }


def model_view(info: ModelInfo) -> dict[str, Any]:
    """Catalogue row: what it is, what it needs, what it costs, what is on offer.

    Confidential pricing exists in the catalogue but the tier is not for sale in
    this release, so it is reported as ``available: false`` with the reason
    attached — a "coming soon" badge in the UI, not a hidden feature.
    """
    confidential_available = False
    return {
        "model_id": info.model_id,
        "name": info.name,
        "format": info.format,
        "size_bytes": info.size_bytes,
        "required_vram_gb": info.required_vram_gb,
        "vram_tier": (
            "small"
            if info.required_vram_gb <= 16
            else "medium"
            if info.required_vram_gb <= 48
            else "large"
        ),
        "license_note": info.license_note,
        "allowlisted": info.allowlisted,
        "standard": {
            "input_micro_per_mtok": info.input_micro_per_mtok,
            "output_micro_per_mtok": info.output_micro_per_mtok,
            "available": bool(info.allowlisted),
        },
        "confidential": {
            "input_micro_per_mtok": info.confidential_input_micro_per_mtok,
            "output_micro_per_mtok": info.confidential_output_micro_per_mtok,
            "available": confidential_available,
            "reason": "coming soon: the confidential tier is not enabled in this release",
        },
        "launch_pricing_until": info.launch_pricing_until,
    }


def job_view(record: JobRecord, node: NodeRecord | None) -> dict[str, Any]:
    """A job row for the usage table and the detail page.

    Region replaces node identity: an operator debugging a job needs to know
    which fleet zone ran it, not which machine, and the console serves
    customers.
    """
    return {
        "job_id": record.job_id,
        "model": record.model,
        "status": record.status,
        "tier": record.tier,
        "confidentiality": record.confidentiality,
        "input_tokens": record.input_tokens,
        "output_tokens": record.output_tokens,
        "total_micro": record.total_micro,
        "hold_micro": record.hold_micro,
        "created_at": record.created_at,
        "settled_at": record.settled_at,
        "region": (node.region if node else "unknown"),
        "gpu_model": (node.gpu_model if node else None),
        "dispute_reason": record.dispute_reason,
    }


def verification_view(record: Any) -> dict[str, Any]:
    """Verification state, in the customer's language, never as a raw status.

    Accepts the store's ``VerificationRecord`` (or ``None`` when the job was
    never sampled). The mapping is explicit so a future status cannot silently
    render as "verified": anything unrecognised reads as pending.
    """
    if record is None:
        return {"state": "not_sampled", "label": "Not sampled"}
    status = str(getattr(record, "status", "") or "").lower()
    outcome = str(getattr(record, "outcome", "") or "").lower()
    if status == "not_dispatched_sdk_mode":
        return {
            "state": "not_sampled",
            "label": "Not sampled (confidential job)",
        }
    if status == "completed":
        if outcome in ("match", "verified", "ok"):
            return {"state": "verified", "label": "Independently verified"}
        return {"state": "mismatch", "label": "Mismatch under review"}
    if status in ("scheduled", "dispatched", "pending"):
        return {"state": "pending", "label": "Sampled, awaiting verification"}
    # Refusal reasons (``no_alternate_node``, …) are not a verification result.
    return {"state": "not_sampled", "label": "Not sampled"}


__all__ = [
    "KES_PER_USD",
    "PACKS",
    "job_view",
    "key_view",
    "model_view",
    "pack",
    "packs",
    "valid_scopes",
    "verification_view",
]
