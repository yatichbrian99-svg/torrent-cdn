import json
import logging
from fastapi import APIRouter, Request
from fastapi.responses import Response

from ..config import get_settings
from ..database import get_redis

logger = logging.getLogger(__name__)
settings = get_settings()
router = APIRouter(prefix="/api", tags=["security"])


@router.post("/csp-report")
async def csp_report_endpoint(request: Request):
    """Receive CSP violation reports from browsers.

    Accepts both the older CSP Level 1 format (application/csp-report)
    and the newer Reporting API format (application/reports+json).
    """
    # IP-based rate limiting for CSP reports
    try:
        redis = await get_redis()
        ip = request.client.host if request.client else "unknown"
        rate_key = f"csp_report_rate:{ip}"
        count = await redis.get(rate_key)
        if count and int(count) >= 100:  # max 100 reports per minute per IP
            return Response(status_code=204)
        await redis.incr(rate_key)
        await redis.expire(rate_key, 60)
    except Exception:
        pass  # Redis unavailable — allow report through

    content_type = request.headers.get("content-type", "").lower()

    try:
        body = await request.body()
        if not body:
            return Response(status_code=204)

        data = json.loads(body)

        if "application/reports+json" in content_type or "application/json" in content_type:
            if isinstance(data, list):
                for report in data:
                    _process_report(report)
            elif isinstance(data, dict):
                _process_report(data)
        else:
            # Legacy CSP report format
            csp_report = data if isinstance(data, dict) else {}
            _process_report({"type": "csp-violation", "body": csp_report})

    except json.JSONDecodeError:
        logger.warning("CSP report endpoint received unparseable body")
    except Exception as e:
        logger.error("Error processing CSP report: %s", e)

    # Always return 204 — no content, no redirects
    return Response(status_code=204)


def _process_report(report: dict):
    """Extract and log the relevant details from a CSP violation report."""
    # Handle the Reporting API format
    if report.get("type") == "csp-violation":
        body = report.get("body", {})
        details = {
            "document_uri": body.get("documentURL"),
            "blocked_uri": body.get("blockedURL"),
            "violated_directive": body.get("violatedDirective"),
            "original_policy": body.get("originalPolicy"),
            "source_file": body.get("sourceFile"),
            "line_number": body.get("lineNumber"),
            "column_number": body.get("columnNumber"),
            "disposition": body.get("disposition"),
        }
    else:
        # Legacy format — the report sits under a "csp-report" key
        csp_report = report.get("csp-report", report)
        details = {
            "document_uri": csp_report.get("document-uri"),
            "blocked_uri": csp_report.get("blocked-uri"),
            "violated_directive": csp_report.get("violated-directive"),
            "original_policy": csp_report.get("original-policy"),
            "source_file": csp_report.get("source-file"),
            "line_number": csp_report.get("line-number"),
            "column_number": csp_report.get("column-number"),
            "disposition": csp_report.get("disposition"),
        }

    # Remove None values for cleaner logging
    details = {k: v for k, v in details.items() if v is not None}

    if details.get("blocked_uri"):
        logger.warning(
            "CSP violation — blocked: %s | directive: %s | document: %s | at %s:%s:%s",
            details.get("blocked_uri"),
            details.get("violated_directive"),
            details.get("document_uri"),
            details.get("source_file", "?"),
            details.get("line_number", "?"),
            details.get("column_number", "?"),
        )
    else:
        logger.info("CSP report received: %s", json.dumps(details))
