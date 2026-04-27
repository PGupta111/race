"""5G results push + Supabase cloud standings.

After each VAR decision, the finish event is:
1. POSTed to RESULTS_WEBHOOK_URL (generic webhook)
2. Upserted to Supabase `race_results` table (if configured)
"""
import logging
import os

import httpx

_WEBHOOK_URL = os.getenv("RESULTS_WEBHOOK_URL", "")
_SUPABASE_URL = os.getenv("SUPABASE_URL", "")
_SUPABASE_KEY = os.getenv("SUPABASE_ANON_KEY", "")
_SUPABASE_TABLE = os.getenv("SUPABASE_TABLE", "race_results")

logger = logging.getLogger(__name__)


async def post_result(event: dict) -> None:
    """Push result to webhook and/or Supabase. Non-blocking with retries."""
    await _push_webhook(event)
    await _push_supabase(event)


async def _push_webhook(event: dict) -> None:
    """Generic webhook POST with 3 retries."""
    if not _WEBHOOK_URL:
        return
    for attempt in range(1, 4):
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.post(_WEBHOOK_URL, json=event)
                r.raise_for_status()
            logger.debug("Webhook delivered event %s (attempt %d)", event.get("id"), attempt)
            return
        except Exception as exc:
            logger.warning(
                "Webhook attempt %d/%d failed for event %s: %s",
                attempt, 3, event.get("id"), exc,
            )
    logger.error("Webhook delivery failed after 3 attempts for event %s", event.get("id"))


async def _push_supabase(event: dict) -> None:
    """Upsert result to Supabase PostgREST. Non-blocking, best-effort."""
    if not _SUPABASE_URL or not _SUPABASE_KEY:
        return

    url = f"{_SUPABASE_URL.rstrip('/')}/rest/v1/{_SUPABASE_TABLE}"
    headers = {
        "apikey": _SUPABASE_KEY,
        "Authorization": f"Bearer {_SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates",
    }
    payload = {
        "event_id": event.get("id"),
        "bib_number": event.get("bib_number"),
        "timestamp": event.get("timestamp"),
        "status": event.get("status"),
        "depth_mm": event.get("depth_mm"),
        "depth_ok": event.get("depth_ok"),
    }

    for attempt in range(1, 4):
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.post(url, json=payload, headers=headers)
                r.raise_for_status()
            logger.info("Supabase upsert OK for event %s", event.get("id"))
            return
        except Exception as exc:
            logger.warning(
                "Supabase attempt %d/%d failed for event %s: %s",
                attempt, 3, event.get("id"), exc,
            )
    logger.error("Supabase push failed after 3 attempts for event %s", event.get("id"))
