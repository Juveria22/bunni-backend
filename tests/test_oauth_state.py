"""
oauth state parameter

this is the only thing binding a google consent screen to a phone number, so a
forgeable or replayable state hands an attacker whichever account signs in
"""

import base64
import json
import time

from services.google_oauth import decode_state, encode_state


def test_state_round_trips():
    assert decode_state(encode_state("+12015551234", "nonce123")) == (
        "+12015551234",
        "nonce123",
    )


def test_tampered_payload_is_rejected():
    """swapping the phone number must not survive the signature"""
    state = encode_state("+12015551234", "nonce123")
    payload, sig = state.rsplit(".", 1)
    forged = json.dumps({"phone": "+19995550000", "nonce": "nonce123", "iat": int(time.time())})
    forged_payload = base64.urlsafe_b64encode(forged.encode()).decode()
    assert decode_state(f"{forged_payload}.{sig}") is None


def test_tampered_signature_is_rejected():
    state = encode_state("+12015551234", "nonce123")
    payload, _ = state.rsplit(".", 1)
    assert decode_state(f"{payload}.{'0' * 32}") is None


def test_expired_state_is_rejected():
    old = json.dumps(
        {"phone": "+12015551234", "nonce": "n", "iat": int(time.time()) - 86400}
    )
    payload = base64.urlsafe_b64encode(old.encode()).decode()
    from services.google_oauth import _sign

    assert decode_state(f"{payload}.{_sign(payload)}") is None


def test_future_dated_state_is_rejected():
    """the window is absolute, so a clock-skewed or forged future iat fails too"""
    ahead = json.dumps(
        {"phone": "+12015551234", "nonce": "n", "iat": int(time.time()) + 86400}
    )
    payload = base64.urlsafe_b64encode(ahead.encode()).decode()
    from services.google_oauth import _sign

    assert decode_state(f"{payload}.{_sign(payload)}") is None


def test_garbage_is_rejected_without_raising():
    for bad in ["", "no-dot", "a.b", "....", "!!!!.!!!!"]:
        assert decode_state(bad) is None


def test_each_link_carries_its_own_nonce():
    """what makes redemption single use once the row is consumed"""
    a = encode_state("+12015551234", "nonce-a")
    b = encode_state("+12015551234", "nonce-b")
    assert a != b
    assert decode_state(a)[1] == "nonce-a"
    assert decode_state(b)[1] == "nonce-b"
