from datetime import datetime, timedelta, timezone

import httpx
import jwt
import pytest
import respx
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from core.errors import FatalError, TransientError
from core.github.app_auth import InstallationTokenCache, make_app_jwt

API = "https://api.github.com"
TOKEN_URL = f"{API}/app/installations/42/access_tokens"


def _test_keypair() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    return private, public


PRIVATE, PUBLIC = _test_keypair()


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}
        self.ttl: dict[str, int | None] = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ex=None):
        self.data[key] = value
        self.ttl[key] = ex


def make_cache(redis, http):
    return InstallationTokenCache(redis, Fernet(Fernet.generate_key()), 123, PRIVATE, http)


def test_jwt_claims():
    token = make_app_jwt(123, PRIVATE, now=1_000_000)
    claims = jwt.decode(token, PUBLIC, algorithms=["RS256"],
                        options={"verify_exp": False, "verify_iat": False})
    assert claims == {"iat": 999_940, "exp": 1_000_540, "iss": "123"}


@respx.mock
async def test_token_fetched_once_then_served_from_cache():
    expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    route = respx.post(TOKEN_URL).respond(201, json={"token": "ghs_secret", "expires_at": expires})
    redis = FakeRedis()
    async with httpx.AsyncClient(base_url=API) as http:
        cache = make_cache(redis, http)
        assert await cache.get(42) == "ghs_secret"
        assert await cache.get(42) == "ghs_secret"

    assert route.call_count == 1
    assert b"ghs_secret" not in redis.data["token:42"]  # stored encrypted
    assert 3200 < redis.ttl["token:42"] <= 3300         # 1h minus the 5 min margin


@respx.mock
async def test_revoked_installation_is_fatal():
    respx.post(TOKEN_URL).respond(404)
    async with httpx.AsyncClient(base_url=API) as http:
        with pytest.raises(FatalError):
            await make_cache(FakeRedis(), http).get(42)


@respx.mock
async def test_github_outage_is_transient():
    respx.post(TOKEN_URL).respond(502)
    async with httpx.AsyncClient(base_url=API) as http:
        with pytest.raises(TransientError):
            await make_cache(FakeRedis(), http).get(42)