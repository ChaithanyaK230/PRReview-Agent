import time
from datetime import datetime, timezone
from typing import Protocol

import httpx
import jwt
from cryptography.fernet import Fernet

from core.errors import FatalError, TransientError

GITHUB_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}

# Refresh installation tokens this many seconds before GitHub expires them
REFRESH_MARGIN_S = 300


class TokenStore(Protocol):
    """The subset of redis.asyncio.Redis we use, so tests can pass a fake."""

    async def get(self, key: str) -> bytes | None: ...
    async def set(self, key: str, value: bytes, ex: int | None = None) -> object: ...


def make_app_jwt(app_id: int, private_key: str, now: int | None = None) -> str:
    """Short-lived JWT proving we are the App (LLD §8.1)."""
    now = int(time.time()) if now is None else now
    # iat is backdated 60s to tolerate clock drift between us and GitHub
    claims = {"iat": now - 60, "exp": now + 540, "iss": str(app_id)}
    return jwt.encode(claims, private_key, algorithm="RS256")


class InstallationTokenCache:
    def __init__(
        self, redis: TokenStore, fernet: Fernet, app_id: int, private_key: str, http: httpx.AsyncClient
    ) -> None:
        self.redis = redis
        self.fernet = fernet
        self.app_id = app_id
        self.private_key = private_key
        self.http = http

    async def get(self, installation_id: int) -> str:
        key = f"token:{installation_id}"
        cached = await self.redis.get(key)
        if cached:
            return self.fernet.decrypt(cached).decode()

        token, expires_at = await self._fetch(installation_id)
        ttl = int((expires_at - datetime.now(timezone.utc)).total_seconds()) - REFRESH_MARGIN_S
        if ttl > 0:
            # Encrypted at rest: a Redis dump never contains a usable token
            await self.redis.set(key, self.fernet.encrypt(token.encode()), ex=ttl)
        return token

    async def _fetch(self, installation_id: int) -> tuple[str, datetime]:
        app_jwt = make_app_jwt(self.app_id, self.private_key)
        try:
            resp = await self.http.post(
                f"/app/installations/{installation_id}/access_tokens",
                headers={**GITHUB_HEADERS, "Authorization": f"Bearer {app_jwt}"},
            )
        except httpx.TransportError as e:
            raise TransientError(f"network error fetching installation token: {e}") from e

        if resp.status_code in (401, 403, 404):
            # App uninstalled, suspended, or wrong key: retrying won't help
            raise FatalError(
                f"installation token request failed: {resp.status_code}",
                user_message="The app could not authenticate with this repository.",
            )
        if resp.status_code >= 500:
            raise TransientError(f"GitHub returned {resp.status_code}")
        resp.raise_for_status()

        data = resp.json()
        return data["token"], datetime.fromisoformat(data["expires_at"])