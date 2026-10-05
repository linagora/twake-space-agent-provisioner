"""The Matrix appservice client, in isolation.

The client's pure parts: the localpart of a bot, the reading of an error body,
and the delay a rate limit asks for. The calls themselves go over HTTP against
the fake homeserver in the seam 2 bench.
"""

from __future__ import annotations

import pytest

from twake_space_agent_provisioner.matrix import (
    MAX_SERVER_DELAY,
    HomeserverError,
    RateLimited,
    bot_localpart,
    classify,
    retry_delay,
)


def test_bot_localpart_is_the_id_without_the_server():
    assert bot_localpart("@twake-space-assistant-jean-dupont:test.invalid") == (
        "twake-space-assistant-jean-dupont"
    )


def test_classify_reads_the_matrix_errcode():
    error = classify(400, {"errcode": "M_INVALID_USERNAME", "error": "bad"})
    assert isinstance(error, HomeserverError)
    assert error.errcode == "M_INVALID_USERNAME"
    assert not error.retryable


def test_classify_marks_a_server_error_retryable():
    assert classify(500, {"errcode": "M_UNKNOWN"}).retryable
    assert classify(502, {}).retryable


def test_classify_marks_a_rate_limit_and_keeps_its_delay():
    error = classify(429, {"errcode": "M_LIMIT_EXCEEDED", "retry_after_ms": 2500})
    assert isinstance(error, RateLimited)
    assert error.retry_after_ms == 2500


def test_classify_marks_a_client_error_permanent():
    assert not classify(403, {"errcode": "M_FORBIDDEN"}).retryable


def test_classify_survives_a_body_that_is_not_json():
    error = classify(503, None)
    assert error.errcode == "M_UNKNOWN"
    assert error.retryable


@pytest.mark.parametrize(
    ("attempt", "expected"),
    [(1, 1.0), (2, 2.0), (3, 4.0), (4, 8.0), (5, 16.0)],
)
def test_retry_delay_doubles_with_the_attempt(attempt: int, expected: float):
    assert retry_delay(attempt, base=1.0, cap=60.0) == expected


def test_retry_delay_is_capped():
    assert retry_delay(20, base=1.0, cap=60.0) == 60.0


def test_retry_delay_honours_the_homeserver_when_it_asks_for_more():
    error = RateLimited(errcode="M_LIMIT_EXCEEDED", retry_after_ms=30_000)
    assert retry_delay(1, base=1.0, cap=60.0, rate_limit=error) == 30.0


def test_retry_delay_keeps_its_own_backoff_when_the_homeserver_asks_for_less():
    error = RateLimited(errcode="M_LIMIT_EXCEEDED", retry_after_ms=200)
    assert retry_delay(3, base=1.0, cap=60.0, rate_limit=error) == 4.0


def test_retry_delay_does_not_clip_a_long_ask_to_our_own_cap():
    """The homeserver's ask wins even past our backoff cap.

    Coming back sooner than asked only prolongs the limit.
    """
    error = RateLimited(errcode="M_LIMIT_EXCEEDED", retry_after_ms=300_000)
    assert retry_delay(1, base=1.0, cap=60.0, rate_limit=error) == 300.0


def test_retry_delay_ignores_a_nonsensical_ask():
    error = RateLimited(errcode="M_LIMIT_EXCEEDED", retry_after_ms=10**12)
    assert retry_delay(1, base=1.0, cap=60.0, rate_limit=error) == MAX_SERVER_DELAY


def test_classify_reads_the_retry_after_header():
    """The newer spelling: Retry-After, in seconds."""
    error = classify(429, {"errcode": "M_LIMIT_EXCEEDED"}, {"Retry-After": "120"})
    assert isinstance(error, RateLimited)
    assert error.retry_after_ms == 120_000


def test_classify_survives_a_bad_retry_after_value():
    for bad in ("soon", "", None):
        error = classify(429, {"errcode": "M_LIMIT_EXCEEDED"}, {"Retry-After": bad})
        assert isinstance(error, RateLimited)
        assert error.retry_after_ms == 0


def test_classify_survives_a_non_numeric_retry_after_ms():
    error = classify(429, {"errcode": "M_LIMIT_EXCEEDED", "retry_after_ms": "later"})
    assert isinstance(error, RateLimited)
    assert error.retry_after_ms == 0


async def test_an_unreachable_homeserver_becomes_a_retryable_error():
    """A transport failure is an outage, not a raw exception.

    The operator's backoff and status reason must see it like any other
    homeserver refusal: letting httpx's exception through would bypass both and
    leave kopf to retry blindly.
    """
    import httpx

    from twake_space_agent_provisioner.matrix import MatrixAppservice

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = MatrixAppservice(
        base_url="http://homeserver.invalid",
        appservice_token="t",
        transport=httpx.MockTransport(refuse),
    )
    try:
        with pytest.raises(HomeserverError) as caught:
            await client.register_bot("@twake-space-assistant-jean-dupont:test.invalid")
    finally:
        await client.close()

    assert caught.value.errcode == "M_UNREACHABLE"
    assert caught.value.retryable


async def test_an_unreachable_homeserver_on_login_becomes_a_retryable_error():
    import httpx

    from twake_space_agent_provisioner.matrix import MatrixAppservice

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    client = MatrixAppservice(
        base_url="http://homeserver.invalid",
        appservice_token="t",
        transport=httpx.MockTransport(refuse),
    )
    try:
        with pytest.raises(HomeserverError) as caught:
            await client.login_bot("@twake-space-assistant-jean-dupont:test.invalid", "D")
    finally:
        await client.close()

    assert caught.value.errcode == "M_UNREACHABLE"
    assert caught.value.retryable
