"""Chat notifications through an incoming webhook (FR-130): Discord and Slack, both free.

One JSON body works for both: Discord reads ``content``, Slack reads ``text``, and each ignores
the other's key. Sending is best-effort with bounded retries (AGENTS.md rule 8) - a dead
webhook must never block or fail the pipeline; it is logged instead.
"""

import asyncio

import httpx

from web_department.config.settings import get_settings
from web_department.logging import get_logger

log = get_logger(__name__)

MAX_ATTEMPTS = 3
TIMEOUT_SECONDS = 10.0
# Discord rejects messages over 2000 characters.
MESSAGE_LIMIT = 1900


async def notify(text: str, *, backoff_seconds: float = 1.0) -> bool:
    """Post ``text`` to ``APPROVAL_WEBHOOK_URL`` if set; True if it was delivered."""
    url = get_settings().approval_webhook_url
    if url is None:
        return False
    message = text if len(text) <= MESSAGE_LIMIT else text[: MESSAGE_LIMIT - 1] + "…"
    body = {"content": message, "text": message}
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = await client.post(url.get_secret_value(), json=body)
                response.raise_for_status()
                return True
            except httpx.HTTPError as exc:
                # Never log the URL: it is the webhook's credential.
                log.warning("webhook_notify_failed", attempt=attempt, error=type(exc).__name__)
                if attempt < MAX_ATTEMPTS:
                    await asyncio.sleep(backoff_seconds * attempt)
    return False
