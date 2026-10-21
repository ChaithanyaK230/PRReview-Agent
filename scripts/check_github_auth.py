import asyncio

import httpx
from cryptography.fernet import Fernet
from redis.asyncio import Redis

from core.github.app_auth import GITHUB_HEADERS, InstallationTokenCache, make_app_jwt
from core.settings import get_settings


async def main() -> None:
    s = get_settings()
    key = s.github_private_key.get_secret_value()
    redis = Redis.from_url(s.redis_url)
    async with httpx.AsyncClient(base_url=s.github_api_url, headers=GITHUB_HEADERS) as http:
        resp = await http.get(
            "/app/installations", headers={"Authorization": f"Bearer {make_app_jwt(s.github_app_id, key)}"}
        )
        resp.raise_for_status()
        cache = InstallationTokenCache(
            redis, Fernet(s.token_encryption_key.get_secret_value()), s.github_app_id, key, http
        )
        for inst in resp.json():
            print(f"installation {inst['id']} on {inst['account']['login']}")
            token = await cache.get(inst["id"])
            repos = await http.get("/installation/repositories", headers={"Authorization": f"token {token}"})
            repos.raise_for_status()
            for repo in repos.json()["repositories"]:
                print(f"  repo: {repo['full_name']}")
    await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())