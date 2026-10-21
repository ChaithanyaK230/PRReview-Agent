import asyncio
import sys

import httpx
from cryptography.fernet import Fernet
from redis.asyncio import Redis

from core.github.app_auth import GITHUB_HEADERS, InstallationTokenCache, make_app_jwt
from core.github.client import GitHubClient
from core.github.diffmap import DiffMap
from core.settings import get_settings


async def main(repo: str, number: int) -> None:
    s = get_settings()
    key = s.github_private_key.get_secret_value()
    redis = Redis.from_url(s.redis_url)
    async with httpx.AsyncClient(base_url=s.github_api_url, headers=GITHUB_HEADERS) as http:
        # Find which installation covers this repo
        resp = await http.get(
            f"/repos/{repo}/installation",
            headers={"Authorization": f"Bearer {make_app_jwt(s.github_app_id, key)}"},
        )
        resp.raise_for_status()
        tokens = InstallationTokenCache(
            redis, Fernet(s.token_encryption_key.get_secret_value()), s.github_app_id, key, http
        )
        gh = GitHubClient(http, tokens, resp.json()["id"])

        pr = await gh.get_pull(repo, number)
        print(f"PR #{pr.number}: {pr.title}  [{pr.state}] by {pr.author}")
        print(f"  base {pr.base_ref}@{pr.base_sha[:7]}  ->  head {pr.head_sha[:7]}")

        diff = DiffMap.from_diff(await gh.get_diff(repo, number))
        for path, lines in diff.added.items():
            print(f"  {path}: {len(lines)} added lines, commentable {len(diff.right[path])}")

        config = await gh.get_file(repo, ".prreview.yml", pr.base_ref)
        print(f"  .prreview.yml on {pr.base_ref}: {'found' if config else 'not present (defaults)'}")
    await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], int(sys.argv[2])))