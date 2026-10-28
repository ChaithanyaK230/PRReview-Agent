"""Live check for Step 18: real PR → tarballs → E2B sandbox → install → offline → run pytest on both sides.

Usage: uv run python -m scripts.check_workspace ChaithanyaK230/prreview-demo 7
"""

import asyncio
import sys
import time

import httpx
from cryptography.fernet import Fernet
from redis.asyncio import Redis

from core.analysis.config import CONFIG_PATH, load_config
from core.analysis.workspace import prepare_workspace
from core.github.app_auth import GITHUB_HEADERS, InstallationTokenCache, make_app_jwt
from core.github.client import GitHubClient
from core.sandbox.e2b import E2BSandbox
from core.settings import get_settings


def ms_since(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


async def main(repo: str, number: int) -> None:
    s = get_settings()
    if s.e2b_api_key is None:
        sys.exit("E2B_API_KEY is not set in .env")
    key = s.github_private_key.get_secret_value()
    redis = Redis.from_url(s.redis_url)
    async with httpx.AsyncClient(base_url=s.github_api_url, headers=GITHUB_HEADERS, timeout=60) as http:
        resp = await http.get(f"/repos/{repo}/installation",
                              headers={"Authorization": f"Bearer {make_app_jwt(s.github_app_id, key)}"})
        resp.raise_for_status()
        tokens = InstallationTokenCache(redis, Fernet(s.token_encryption_key.get_secret_value()),
                                        s.github_app_id, key, http)
        gh = GitHubClient(http, tokens, resp.json()["id"])

        pr = await gh.get_pull(repo, number)
        print(f"PR #{pr.number}: {pr.title}   base {pr.base_sha[:7]} -> head {pr.head_sha[:7]}")

        started = time.perf_counter()
        base_tar, head_tar = await asyncio.gather(
            gh.download_tarball(repo, pr.base_sha), gh.download_tarball(repo, pr.head_sha))
        print(f"tarballs: base {len(base_tar)} B, head {len(head_tar)} B  ({ms_since(started)} ms)")
        config = load_config(await gh.get_file(repo, CONFIG_PATH, pr.base_ref))

        started = time.perf_counter()
        async with await E2BSandbox.create(s.e2b_api_key.get_secret_value(), lifetime_s=600) as sbx:
            print(f"sandbox {sbx.sandbox_id} created ({ms_since(started)} ms)")

            started = time.perf_counter()
            ws = await prepare_workspace(sbx, base_tar, head_tar, config)
            print(f"workspace ready ({ms_since(started)} ms), tools ok: {ws.tools_ok}")
            for side in (ws.base, ws.head):
                status = "installed" if side.installed else f"NOT installed: {side.log[:200]}"
                print(f"  {side.side}: strategy={side.toolchain.strategy}, {status} ({side.duration_ms} ms)")

            if ws.can_run_tests:  # internet is off from here on
                for side in (ws.base, ws.head):
                    result = await sbx.exec([side.python, "-m", "pytest", "-q", "--no-header"],
                                            cwd=side.root, timeout_s=120)
                    lines = result.stdout.strip().splitlines() or ["(no output)"]
                    print(f"  pytest on {side.side}: exit {result.exit_code}  {lines[-1]}")
                    if result.exit_code >= 2:  # 2+ = tests couldn't even be collected/run: show why
                        print("    " + "\n    ".join(lines[-15:]))
        print("sandbox killed")
    await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], int(sys.argv[2])))
