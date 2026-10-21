import httpx
from arq.connections import RedisSettings
from cryptography.fernet import Fernet
from openai import AsyncOpenAI

from core.agent.llm import LLM
from core.db.session import get_sessionmaker
from core.github.app_auth import GITHUB_HEADERS, InstallationTokenCache
from core.github.client import GitHubClient
from core.settings import get_settings
from worker.orchestrator import run_review


async def startup(ctx: dict) -> None:
    s = get_settings()
    http = httpx.AsyncClient(base_url=s.github_api_url, headers=GITHUB_HEADERS, timeout=30)
    tokens = InstallationTokenCache(
        ctx["redis"],  # arq's own Redis connection doubles as the token cache
        Fernet(s.token_encryption_key.get_secret_value()),
        s.github_app_id,
        s.github_private_key.get_secret_value(),
        http,
    )
    # max_retries=0: the orchestrator owns retries (with_retries), so the SDK must not retry too
    llm_client = AsyncOpenAI(
        base_url=s.llm_base_url, api_key=s.openrouter_api_key.get_secret_value(), max_retries=0, timeout=120,
    )
    ctx["http"] = http
    ctx["llm_client"] = llm_client
    ctx["sessionmaker"] = get_sessionmaker()
    ctx["gh_for"] = lambda installation_id: GitHubClient(http, tokens, installation_id)
    ctx["llm_for"] = lambda job_id: LLM(llm_client, s.llm_model, job_id)


async def shutdown(ctx: dict) -> None:
    await ctx["http"].aclose()
    await ctx["llm_client"].close()


class WorkerSettings:
    functions = [run_review]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_jobs = get_settings().worker_concurrency
    job_timeout = 15 * 60
    max_tries = 3
    keep_result = 3600
    handle_signals = False  # Windows event loops don't support signal handlers
