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
from core.analysis.delta import compute_delta, failure_signature
from core.analysis.runners import run_analysis
from core.github.diffmap import DiffMap
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
        diff = DiffMap.from_diff(await gh.get_diff(repo, number))

        started = time.perf_counter()
        async with await E2BSandbox.create(s.e2b_api_key.get_secret_value(), lifetime_s=600) as sbx:
            print(f"sandbox {sbx.sandbox_id} created ({ms_since(started)} ms)")

            started = time.perf_counter()
            ws = await prepare_workspace(sbx, base_tar, head_tar, config)
            print(f"workspace ready ({ms_since(started)} ms), tools ok: {ws.tools_ok}")
            for side in (ws.base, ws.head):
                status = "installed" if side.installed else f"NOT installed: {side.log[:200]}"
                print(f"  {side.side}: strategy={side.toolchain.strategy}, {status} ({side.duration_ms} ms)")

            # Step 19: structured results (internet is off from here on)
            analysis = await run_analysis(sbx, ws, config)
            for side, outcomes in (("base", analysis.base), ("head", analysis.head)):
                for o in outcomes:
                    state = "ok" if o.available else f"UNAVAILABLE ({o.log[:120]})"
                    print(f"  [{side}] {o.tool}: {state}, exit {o.exit_code}, {o.duration_ms} ms")
                    for f in o.findings:
                        print(f"      {f.severity:<7} {f.rule:<14} {f.path}:{f.line}  {f.message[:70]}")
                    for t in o.tests:
                        first = (t.message or "").strip().splitlines()[-1:] or [""]
                        print(f"      {t.outcome:<7} {t.test_id}  {first[0][:90]}")

            # Step 20: what did THIS PR introduce?
            delta = compute_delta(analysis.base, analysis.head, diff)
            print("  DELTA (blamed on this PR):")
            for f in delta.new_findings:
                print(f"      new finding    {f.rule} {f.path}:{f.line}  {f.message[:70]}")
            for c in delta.test_changes:
                print(f"      {c.kind:<16} {c.test.test_id}  {failure_signature(c.test.message)[:80]}")
            if not (delta.new_findings or delta.test_changes):
                print("      (nothing new)")
            if delta.fixed_tests:
                print(f"      fixed by this PR: {', '.join(delta.fixed_tests)}")
            if delta.unavailable:
                print(f"      not compared (missing evidence): {', '.join(delta.unavailable)}")
        print("sandbox killed")
    await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], int(sys.argv[2])))
