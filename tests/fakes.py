"""
in-memory stand-ins for the services the webhook path talks to

the point of these is to leave the real control flow alone. the gates in
receive_message run their actual redis logic against FakeRedis rather than
being mocked out, because the ordering of those gates is the thing worth
testing and a mock would assert nothing
"""

from contextlib import asynccontextmanager


class FakeRedis:
    """Enough of the redis surface for the rate limit, locks and spend counter."""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.expiries: dict[str, int] = {}

    async def set(self, key, value, ex=None, nx=False, **kwargs):
        if nx and key in self.store:
            return None
        self.store[key] = str(value)
        if ex is not None:
            self.expiries[key] = ex
        return True

    async def get(self, key):
        return self.store.get(key)

    async def incr(self, key):
        value = int(self.store.get(key, 0)) + 1
        self.store[key] = str(value)
        return value

    async def incrby(self, key, amount):
        value = int(self.store.get(key, 0)) + amount
        self.store[key] = str(value)
        return value

    async def ttl(self, key):
        if key not in self.store:
            return -2
        return self.expiries.get(key, -1)

    async def expire(self, key, seconds):
        self.expiries[key] = seconds
        return True


class BrokenRedis:
    """Every call fails, for exercising the degraded paths."""

    def __getattr__(self, _name):
        async def _boom(*args, **kwargs):
            raise ConnectionError("redis is down")

        return _boom


class FakeUser:
    def __init__(self, onboarded=True, token="stored-refresh-token"):
        self.google_refresh_token = token if onboarded else None
        self.google_email = "user@example.test" if onboarded else None
        self.channel = "sms"

    @property
    def is_onboarded(self):
        return self.google_refresh_token is not None


@asynccontextmanager
async def fake_get_db():
    """The handler only passes the session through to patched repo calls."""
    yield object()
