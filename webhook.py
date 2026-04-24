"""5G results push — POST finish events to RESULTS_WEBHOOK_URL after each VAR decision."""
import logging
import os

import httpx

_WEBHOOK_URL = os.getenv("RESULTS_WEBHOOK_URL", "")
logger = logging.getLogger(__name__)


async def post_result(event: dict) -> None:
    """Non-blocking: retries up to 3 times with a 5-second timeout each."""
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
