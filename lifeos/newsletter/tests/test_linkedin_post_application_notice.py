from datetime import datetime, timezone

from lifeos.newsletter.models import ParseState, RoutedNewsletterMessage
from lifeos.newsletter.processor import _parse_message_or_known_empty


def _message(subject: str, body: str) -> RoutedNewsletterMessage:
    return RoutedNewsletterMessage(
        mailbox="gmail",
        message_id="synthetic-linkedin-post-application",
        received_at=datetime(2026, 9, 17, tzinfo=timezone.utc),
        sender="Synthetic LinkedIn notification via linkedin.com",
        subject=subject,
        body_text=body,
    )


def test_linkedin_post_application_networking_notice_is_terminal_empty_pass() -> None:
    result = _parse_message_or_known_empty(
        _message(
            "Message people you know at Synthetic Co to learn more",
            "Now that you’ve applied to Project Manager at Synthetic Co, message your connections to learn more about the company.",
        )
    )
    assert result.state is ParseState.PASS
    assert result.observations == ()
    assert result.issues == ()


def test_unrecognized_linkedin_message_still_fails_closed() -> None:
    result = _parse_message_or_known_empty(
        _message("A LinkedIn notification", "There are no vacancy cards in this synthetic message.")
    )
    assert result.state is ParseState.DEGRADED
    assert result.observations == ()
