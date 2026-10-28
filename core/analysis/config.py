from fnmatch import fnmatch
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from core.errors import FatalError

CONFIG_PATH = ".prreview.yml"


class RepoConfig(BaseModel):
    """Per-repo settings from .prreview.yml, always read from the BASE branch (LLD §10.1)."""

    model_config = ConfigDict(extra="forbid")  # a typo'd key is an error, not silently ignored

    language: Literal["python", "typescript", "auto"] = "auto"
    install: list[str] | None = None  # shell commands that replace auto-detected install
    lint: list[Literal["ruff", "mypy", "eslint", "tsc", "semgrep"]] | None = None
    test: list[str] | None = None
    test_timeout_s: int = Field(360, ge=10, le=600)
    ignore_paths: list[str] = ["**/migrations/**", "**/*.lock", "**/dist/**", "**/vendor/**"]
    max_comments: int = Field(7, ge=1, le=15)
    categories: set[Literal["bug", "security", "test_gap", "perf"]] = {"bug", "security", "test_gap", "perf"}
    proof_tests: bool = True

    def is_ignored(self, path: str) -> bool:
        # Leading "/" so "**/migrations/**" also matches a top-level "migrations/x.py"
        return any(fnmatch(f"/{path}", pattern) for pattern in self.ignore_paths)


def load_config(text: str | None) -> RepoConfig:
    """Parse .prreview.yml. Missing or empty file → defaults; invalid file → FatalError (LLD §7.4)."""
    if not text or not text.strip():
        return RepoConfig()
    try:
        data = yaml.safe_load(text)  # safe_load: never constructs arbitrary Python objects from YAML tags
    except yaml.YAMLError as e:
        raise FatalError(f"{CONFIG_PATH} is not valid YAML: {e}",
                         user_message=f"{CONFIG_PATH} is not valid YAML.") from e
    if data is None:
        return RepoConfig()
    if not isinstance(data, dict):
        raise FatalError(f"{CONFIG_PATH} must be a mapping", user_message=f"{CONFIG_PATH} must be a YAML mapping.")
    try:
        return RepoConfig.model_validate(data)
    except ValidationError as e:
        first = e.errors()[0]
        where = ".".join(str(p) for p in first["loc"]) or "(root)"
        raise FatalError(f"{CONFIG_PATH} invalid: {e}",
                         user_message=f"{CONFIG_PATH} is invalid at `{where}`: {first['msg']}") from e
