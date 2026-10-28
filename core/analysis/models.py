from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel


class Finding(BaseModel):
    """One lint/type/security problem, from any tool (LLD §10.3)."""

    tool: str
    rule: str
    path: str  # relative to the repo root
    line: int
    end_line: int | None = None
    severity: Literal["error", "warning", "info"]
    message: str
    fingerprint: str = ""  # filled by the delta step (Step 20)


class TestResult(BaseModel):
    """One test's outcome. test_id looks like "tests/test_x.py::TestCls::test_y"."""

    __test__ = False  # stop pytest from trying to collect this class because its name starts with "Test"

    test_id: str
    outcome: Literal["passed", "failed", "error", "skipped"]
    message: str | None = None


@dataclass(frozen=True)
class ToolOutcome:
    """One tool run on one side. available=False means "no evidence" (timeout, crash), not "no problems"."""

    tool: str
    side: str  # base | head
    exit_code: int
    duration_ms: int
    timed_out: bool
    available: bool
    findings: list[Finding] = field(default_factory=list)
    tests: list[TestResult] = field(default_factory=list)
    log: str = ""  # short reason when unavailable / tail of output
