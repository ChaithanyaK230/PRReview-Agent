import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Protocol

from pydantic import BaseModel

from core.agent.schemas import CandidateComment
from core.analysis.config import RepoConfig
from core.analysis.models import TestResult
from core.analysis.runners.pytest import run_pytest
from core.analysis.workspace import Workspace
from core.github.diffmap import DiffMap
from core.sandbox.base import WORK_ROOT, Sandbox


class CriticVerdict(BaseModel):
    """Result of one critic on one candidate (LLD §13.1; named to avoid clashing with the critic_results table)."""

    critic: str
    passed: bool
    feedback: str = ""  # on failure: written for the reviser LLM, so it says what to change
    verified: bool = False
    duration_ms: int = 0


@dataclass(frozen=True)
class Attempt:
    """Which candidate and which reflection round a critic is checking."""

    candidate_id: uuid.UUID
    iteration: int


@dataclass
class CriticContext:
    """Everything critics may need. Sandbox fields are None in a diff-only review."""

    diff: DiffMap
    config: RepoConfig
    sandbox: Sandbox | None = None
    workspace: Workspace | None = None
    head_tests: list[TestResult] | None = None  # from the analyze step; None = not known
    _baseline: set[str] | None = field(default=None, init=False)
    _baseline_ready: bool = field(default=False, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    @property
    def can_execute(self) -> bool:
        return self.sandbox is not None and self.workspace is not None and self.workspace.tools_ok

    def scratch_dir(self, attempt: Attempt) -> str:
        # One folder per candidate per round (LLD §9.3), so parallel candidates never touch each other
        return f"{WORK_ROOT}/scratch/{attempt.candidate_id.hex[:12]}-{attempt.iteration}"

    async def passing_head_tests(self) -> set[str] | None:
        """Tests that pass on head without any patch; None if unknown. Computed at most once per job."""
        async with self._lock:  # several candidates may ask at the same time
            if not self._baseline_ready:
                self._baseline_ready = True
                tests = self.head_tests
                if tests is None and self.can_execute and self.workspace.head.installed:
                    # The job resumed after a crash: analyze's results aren't in memory, so re-run once
                    outcome = await run_pytest(self.sandbox, self.workspace.head, self.config)
                    tests = outcome.tests if outcome.available else None
                if tests is not None:
                    self._baseline = {t.test_id for t in tests if t.outcome == "passed"}
        return self._baseline


class Critic(Protocol):
    name: str

    def applies(self, c: CandidateComment, ctx: CriticContext) -> bool: ...

    async def check(
        self, c: CandidateComment, ctx: CriticContext, attempt: Attempt
    ) -> tuple[CandidateComment, CriticVerdict]:
        """Return the (possibly adjusted) comment and a verdict. Never raise for a failed check."""
        ...
