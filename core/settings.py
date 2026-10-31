from decimal import Decimal
from functools import lru_cache
from typing import Annotated


from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

class Settings(BaseSettings):
    model_config=SettingsConfigDict(env_file=".env",env_file_encoding="utf-8",extra="ignore",hide_input_in_errors=True)

    # GitHub App
    github_app_id: int
    github_private_key: SecretStr
    github_webhook_secret: SecretStr
    bot_login: str = "prreview[bot]"
    github_api_url: str = "https://api.github.com"
    token_encryption_key: SecretStr  # Fernet key for encrypting cached installation tokens

    # Infrastructure
    database_url: str
    redis_url: str

    # LLM: Claude models via OpenRouter's Anthropic-compatible endpoint
    openrouter_api_key: SecretStr
    llm_base_url: str = "https://openrouter.ai/api/v1"
    llm_model:str


    # Not needed until later phases
    e2b_api_key: SecretStr | None = None
    s3_bucket: str | None = None
    s3_endpoint: str | None = None


    # Limits (LLD §3 defaults)
    max_iterations: int = 3
    max_comments: int = 7
    job_budget_usd: Decimal = Decimal("0.50")
    daily_budget_usd: Decimal = Decimal("20.00")
    install_daily_quota: int = 30
    worker_concurrency: int = 4
    embedded_worker: bool = False  # run the worker inside the API process (one free web service, e.g. Render)
    kill_switch: bool = False
    demo_repos: Annotated[list[str], NoDecode] = []  # repos whose traces the dashboard shows publicly
    public_url: str | None = None  # e.g. https://prreview.fly.dev; adds "View full trace" to summaries

    @field_validator("demo_repos", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        # DEMO_REPOS=owner/a,owner/b
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v

@lru_cache
def get_settings() -> Settings:
    return Settings()
    




