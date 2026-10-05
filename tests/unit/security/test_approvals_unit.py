"""Approval notifications and request validation (FR-130, FR-131)."""

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from web_department.api.schemas import ApprovalDecisionRequest
from web_department.collaboration import notifications
from web_department.config.settings import get_settings
from web_department.db.models import Approval
from web_department.security.approvals import Decision, notification_text

WEBHOOK = "https://discord.com/api/webhooks/123/very-secret-token"


class FakeWebhook:
    """Records every POST; the first ``failures`` of them answer 500."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.failures = 0


@pytest.fixture
def webhook(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeWebhook]:
    monkeypatch.setenv("APPROVAL_WEBHOOK_URL", WEBHOOK)
    get_settings.cache_clear()
    fake = FakeWebhook()

    async def post(self: httpx.AsyncClient, url: str, *, json: dict[str, Any]) -> httpx.Response:
        fake.sent.append({"url": url, "json": json})
        status = 500 if len(fake.sent) <= fake.failures else 204
        return httpx.Response(status, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    yield fake
    get_settings.cache_clear()


async def test_notify_posts_one_body_that_discord_and_slack_both_read(
    webhook: FakeWebhook,
) -> None:
    assert await notifications.notify("Approval needed") is True

    assert webhook.sent == [
        {"url": WEBHOOK, "json": {"content": "Approval needed", "text": "Approval needed"}}
    ]


async def test_notify_retries_a_bounded_number_of_times_then_gives_up(
    webhook: FakeWebhook, capsys: pytest.CaptureFixture[str]
) -> None:
    webhook.failures = 99

    delivered = await notifications.notify("x", backoff_seconds=0)

    assert delivered is False
    assert len(webhook.sent) == notifications.MAX_ATTEMPTS
    assert "very-secret-token" not in capsys.readouterr().out  # the URL is a credential


async def test_notify_recovers_after_a_transient_failure(webhook: FakeWebhook) -> None:
    webhook.failures = 1

    assert await notifications.notify("x", backoff_seconds=0) is True
    assert len(webhook.sent) == 2


async def test_notify_is_a_no_op_without_a_webhook(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APPROVAL_WEBHOOK_URL", "")
    get_settings.cache_clear()
    try:
        assert await notifications.notify("x") is False
    finally:
        get_settings.cache_clear()


async def test_long_messages_are_cut_to_discord_s_limit(webhook: FakeWebhook) -> None:
    await notifications.notify("y" * 5000)

    assert len(webhook.sent[0]["json"]["content"]) == notifications.MESSAGE_LIMIT


def test_notification_text_says_what_and_where_to_decide() -> None:
    approval = Approval(
        id="apr_1", project_id="prj_1", action_type="production_deploy", summary="Deploy v1"
    )

    text = notification_text(approval, "Clothing shop")

    assert "Approval needed: production_deploy for project 'Clothing shop'" in text
    assert "GET http://localhost:8000/api/v1/approvals/apr_1" in text
    assert "POST http://localhost:8000/api/v1/approvals/apr_1" in text


def test_a_rejection_must_say_what_to_change() -> None:
    with pytest.raises(ValidationError, match="needs a comment"):
        ApprovalDecisionRequest(decision=Decision.REJECT, comment="  ", approver_id="a")

    approve = ApprovalDecisionRequest(decision=Decision.APPROVE, approver_id="a")
    assert approve.comment is None
