"""
the inbound webhook path, end to end

these cover the gates in receive_message and the order they run in, which is
where the money and the compliance obligations both live:

  signature -> dedup -> STOP -> HELP -> rate limit -> budget -> agent

everything before the agent is reached without a model call, so the whole file
runs offline. the redis logic is real, running against an in-memory fake
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import sms as sms_router
from services import budget, monitoring, rate_limit, redis_client
from tests.fakes import BrokenRedis, FakeRedis, FakeUser, fake_get_db


@pytest.fixture
def redis(monkeypatch):
    """a fresh in-memory redis, and module state reset between tests"""
    fake = FakeRedis()
    monkeypatch.setattr(redis_client, "_redis", fake)

    # these cache or accumulate across calls by design
    monkeypatch.setattr(budget, "_cached_spend", 0)
    monkeypatch.setattr(budget, "_cached_at", 0.0)
    monkeypatch.setattr(budget, "_local_spend", {})
    monkeypatch.setattr(budget, "_announced", set())
    monkeypatch.setattr(rate_limit, "_local_counts", {})
    monkeypatch.setattr(rate_limit, "_local_help", {})
    monkeypatch.setattr(monitoring, "_counts", monitoring.Counter())
    monkeypatch.setattr(monitoring, "_totals", monitoring.Counter())
    return fake


@pytest.fixture
def spawned(monkeypatch):
    """capture agent hand-offs instead of running the model"""
    calls = []
    monkeypatch.setattr(
        sms_router, "_spawn_reply", lambda *a, **k: calls.append(a)
    )
    return calls


@pytest.fixture
def opted_out(monkeypatch):
    """capture opt-out deletions"""
    deleted = []

    async def fake_delete(db, phone):
        deleted.append(phone)

    async def fake_get_user(db, phone):
        return FakeUser()

    monkeypatch.setattr(sms_router, "delete_user_data", fake_delete)
    monkeypatch.setattr(sms_router, "get_user", fake_get_user)
    monkeypatch.setattr(sms_router, "_spawn_revoke", lambda token: None)
    return deleted


@pytest.fixture
def client(monkeypatch, redis):
    """
    a bare app carrying only the sms router

    main.app is avoided here so the test never touches lifespan, which would
    try to reach postgres. the size middleware is covered separately
    """
    monkeypatch.setattr(sms_router, "VALIDATE_SIGNATURE", False)
    monkeypatch.setattr(sms_router, "get_db", fake_get_db)

    async def fake_get_or_create(db, phone, channel=None):
        return FakeUser(), False

    async def fake_auth_url(db, phone):
        return "https://example.test/auth?state=abc"

    monkeypatch.setattr(sms_router, "get_or_create_user", fake_get_or_create)
    monkeypatch.setattr(sms_router, "generate_auth_url", fake_auth_url)
    monkeypatch.setattr(sms_router, "decrypt_token", lambda stored: stored)

    app = FastAPI()
    app.include_router(sms_router.router)
    return TestClient(app)


def post(client, body="dentist friday at 3", sid="SM1", frm="+12015551234"):
    return client.post(
        "/message", data={"From": frm, "Body": body, "MessageSid": sid}
    )


# ---------- signature ----------

def test_unsigned_request_is_rejected(monkeypatch, client):
    monkeypatch.setattr(sms_router, "VALIDATE_SIGNATURE", True)
    assert post(client).status_code == 403


def test_rejected_request_never_reaches_the_agent(monkeypatch, client, spawned):
    monkeypatch.setattr(sms_router, "VALIDATE_SIGNATURE", True)
    post(client)
    assert spawned == []


# ---------- duplicate delivery ----------

def test_first_delivery_reaches_the_agent(client, spawned):
    assert post(client, sid="SM-dup").status_code == 200
    assert len(spawned) == 1


def test_redelivery_of_the_same_sid_is_dropped(client, spawned):
    """twilio is at-least-once, a second run would double the bill"""
    post(client, sid="SM-dup")
    post(client, sid="SM-dup")
    assert len(spawned) == 1


def test_different_messages_are_not_deduped(client, spawned):
    post(client, sid="SM-a")
    post(client, sid="SM-b")
    assert len(spawned) == 2


def test_a_missing_sid_still_gets_processed(client, spawned):
    """the field is optional, absence must not silently drop real traffic"""
    client.post("/message", data={"From": "+12015551234", "Body": "hi"})
    assert len(spawned) == 1


# ---------- STOP ----------

def test_stop_deletes_everything(client, opted_out):
    post(client, body="STOP", sid="SM-stop")
    assert opted_out == ["+12015551234"]


def test_stop_sends_nothing_back(client, opted_out):
    """twilio blocks outbound after STOP, a reply would just be a wasted segment"""
    body = post(client, body="STOP", sid="SM-stop").text
    assert "<Message>" not in body


def test_stop_is_case_and_punctuation_insensitive(client, opted_out):
    post(client, body="  Stop! ", sid="SM-stop2")
    assert opted_out == ["+12015551234"]


def test_stop_never_reaches_the_agent(client, opted_out, spawned):
    post(client, body="STOP", sid="SM-stop3")
    assert spawned == []


async def test_stop_still_works_when_the_budget_is_gone(redis, client, opted_out):
    """leaving must not depend on there being money left"""
    await budget.record_sms(10_000)
    budget._cached_at = 0.0
    post(client, body="STOP", sid="SM-stop4")
    assert opted_out == ["+12015551234"]


# ---------- HELP ----------

def test_help_is_answered(client):
    assert "text me plain english" in post(client, body="HELP", sid="SM-h1").text


def test_help_is_answered_only_once_per_window(client):
    """it sits ahead of the rate limit, so it needs its own ceiling"""
    first = post(client, body="HELP", sid="SM-h1").text
    second = post(client, body="HELP", sid="SM-h2").text
    assert "text me plain english" in first
    assert "<Message>" not in second


def test_help_never_reaches_the_agent(client, spawned):
    post(client, body="HELP", sid="SM-h1")
    assert spawned == []


async def test_help_still_works_when_the_budget_is_gone(redis, client):
    """answering HELP is a carrier requirement, not discretionary spend"""
    await budget.record_sms(10_000)
    budget._cached_at = 0.0
    assert "text me plain english" in post(client, body="HELP", sid="SM-h3").text


# ---------- rate limit ----------

def test_traffic_under_the_limit_is_handled(client, spawned):
    for i in range(rate_limit.RATE_LIMIT):
        post(client, sid=f"SM-{i}")
    assert len(spawned) == rate_limit.RATE_LIMIT


def test_first_message_over_the_limit_is_answered_then_silence(client, spawned):
    """replying every time bills a segment per attempt just to say slow down"""
    for i in range(rate_limit.RATE_LIMIT):
        post(client, sid=f"SM-{i}")

    over = post(client, sid="SM-over-1").text
    further = post(client, sid="SM-over-2").text

    assert "one at a time bestie" in over
    assert "<Message>" not in further
    assert len(spawned) == rate_limit.RATE_LIMIT


def test_rate_limited_traffic_never_reaches_the_agent(client, spawned):
    for i in range(rate_limit.RATE_LIMIT + 5):
        post(client, sid=f"SM-{i}")
    assert len(spawned) == rate_limit.RATE_LIMIT


def test_the_limit_is_per_phone_number(client, spawned):
    for i in range(rate_limit.RATE_LIMIT + 2):
        post(client, sid=f"SM-a{i}", frm="+12015551111")
    post(client, sid="SM-b1", frm="+12015552222")
    assert len(spawned) == rate_limit.RATE_LIMIT + 1


# ---------- budget ----------

async def test_budget_cutoff_stops_the_agent(redis, client, spawned):
    await budget.record_sms(10_000)   # far past a $15 ceiling
    budget._cached_at = 0.0
    post(client, sid="SM-budget")
    assert spawned == []


async def test_budget_cutoff_explains_itself_once_per_day(redis, client):
    await budget.record_sms(10_000)
    budget._cached_at = 0.0

    first = post(client, sid="SM-b1").text
    second = post(client, sid="SM-b2").text

    assert "maxed out" in first
    assert "<Message>" not in second


async def test_spending_under_the_ceiling_changes_nothing(redis, client, spawned):
    await budget.record_sms(1)
    budget._cached_at = 0.0
    post(client, sid="SM-cheap")
    assert len(spawned) == 1


# ---------- onboarding ----------

def test_a_new_user_gets_an_auth_link_not_an_agent_run(monkeypatch, client, spawned):
    async def not_onboarded(db, phone, channel=None):
        return FakeUser(onboarded=False), True

    monkeypatch.setattr(sms_router, "get_or_create_user", not_onboarded)
    body = post(client, sid="SM-new").text

    assert "example.test/auth" in body
    assert spawned == []


def test_reconnect_keyword_returns_a_fresh_link(client, spawned):
    body = post(client, body="reconnect", sid="SM-recon").text
    assert "example.test/auth" in body
    assert spawned == []


def test_an_unreadable_token_offers_a_reconnect_instead_of_failing(
    monkeypatch, client, spawned
):
    """a rotated encryption key must not look like a crash to the user"""
    monkeypatch.setattr(sms_router, "decrypt_token", lambda stored: None)
    body = post(client, sid="SM-badkey").text
    assert "reconnect" in body
    assert spawned == []


# ---------- degraded redis ----------

def test_redis_down_still_answers_rather_than_dropping_the_message(
    monkeypatch, client, spawned
):
    """
    the fallbacks are deliberate: a duplicate reply or a looser rate limit is
    better than silently losing someone's only text
    """
    monkeypatch.setattr(redis_client, "_redis", BrokenRedis())
    assert post(client, sid="SM-noredis").status_code == 200
    assert len(spawned) == 1


# ---------- the /sms alias ----------

def test_the_sms_alias_behaves_identically(client, spawned):
    resp = client.post(
        "/sms",
        data={"From": "+12015551234", "Body": "dentist friday", "MessageSid": "SM-alias"},
    )
    assert resp.status_code == 200
    assert len(spawned) == 1


def test_the_alias_shares_the_dedup_gate(client, spawned):
    data = {"From": "+12015551234", "Body": "hi", "MessageSid": "SM-shared"}
    client.post("/message", data=data)
    client.post("/sms", data=data)
    assert len(spawned) == 1
