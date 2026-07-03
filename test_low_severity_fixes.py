"""Focused unit tests for the low-severity runtime-hardening fixes:

* Retry-After parsing now honors the HTTP-date form and clamps negatives.
* JWT decode enforces `nbf` and allows a configurable clock-skew `leeway`.
"""

import time
import types

import pytest

from orchestrator import _parse_retry_after, wait_strategy
from ag_core.utils.jwt import decode_jwt, encode_jwt

import httpx


SECRET = "low-sev-test-key"


# --- Retry-After parsing -------------------------------------------------


def test_parse_retry_after_delta_seconds():
    assert _parse_retry_after("5") == 5.0


def test_parse_retry_after_http_date_future_is_positive():
    future = time.time() + 30
    http_date = time.strftime(
        "%a, %d %b %Y %H:%M:%S GMT", time.gmtime(future)
    )
    delay = _parse_retry_after(http_date)
    assert delay is not None and delay > 0


def test_parse_retry_after_http_date_past_is_negative():
    past = time.time() - 300
    http_date = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(past))
    delay = _parse_retry_after(http_date)
    assert delay is not None and delay < 0


def test_parse_retry_after_garbage_is_none():
    assert _parse_retry_after("not-a-date") is None


def _retry_state_with_retry_after(value):
    resp = httpx.Response(429, headers={"Retry-After": value})
    exc = httpx.HTTPStatusError("429", request=httpx.Request("GET", "http://x"), response=resp)
    outcome = types.SimpleNamespace(exception=lambda: exc)
    return types.SimpleNamespace(outcome=outcome, attempt_number=1)


def test_wait_strategy_clamps_past_http_date_to_zero():
    past = time.time() - 300
    http_date = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(past))
    # A past HTTP-date -> negative delay -> must be floored at 0, never negative.
    assert wait_strategy(_retry_state_with_retry_after(http_date)) == 0.0


def test_wait_strategy_caps_at_60():
    assert wait_strategy(_retry_state_with_retry_after("9999")) == 60.0


# --- JWT nbf + leeway ----------------------------------------------------


def test_jwt_nbf_rejected_when_far_in_future():
    token = encode_jwt({"sub": "w", "nbf": time.time() + 3600}, SECRET)
    with pytest.raises(ValueError, match="not yet valid"):
        decode_jwt(token, SECRET, leeway=0)


def test_jwt_nbf_within_leeway_accepted():
    token = encode_jwt({"sub": "w", "nbf": time.time() + 5}, SECRET)
    payload = decode_jwt(token, SECRET, leeway=60)
    assert payload["sub"] == "w"


def test_jwt_exp_within_leeway_accepted():
    # Expired 5s ago but within a 60s skew allowance -> accepted.
    token = encode_jwt({"sub": "w", "exp": time.time() - 5}, SECRET)
    payload = decode_jwt(token, SECRET, leeway=60)
    assert payload["sub"] == "w"


def test_jwt_exp_rejected_with_zero_leeway():
    token = encode_jwt({"sub": "w", "exp": time.time() - 5}, SECRET)
    with pytest.raises(ValueError, match="expired"):
        decode_jwt(token, SECRET, leeway=0)
