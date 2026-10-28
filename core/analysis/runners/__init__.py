from dataclasses import dataclass

import structlog

from core.analysis.config import RepoConfig
from core.analysis.models import ToolOutcome
from core.analysis.runners.pytest import run_pytest
from core.analysis.runners.ruff import run_ruff
from core.analysis.workspace import Workspace
from core.sandbox.base import Sandbox

log = structlog.get_logger()


@dataclass(frozen=True)
class Analysis:
    base: list[ToolOutcome]
    head: list[ToolOutcome]


async def run_analysis(sbx: Sandbox, ws: Workspace, config: RepoConfig) -> Analysis:
    """Run every tool on base, then head. Sequential so both sides get the same CPU conditions.

    TODO: semgrep and mypy (LLD §10.3) — semgrep's install is large; added once the basics work.
    """
    sides = {}
    for setup in (ws.base, ws.head):
        outcomes = []
        if ws.tools_ok and setup.toolchain.supported:
            outcomes.append(await run_ruff(sbx, setup, config))  # needs only the shared tools venv
        if setup.installed:
            outcomes.append(await run_pytest(sbx, setup, config))  # needs this side's dependencies
        for o in outcomes:
            log.info("analysis.tool", tool=o.tool, side=o.side, available=o.available, exit=o.exit_code,
                     findings=len(o.findings), tests=len(o.tests), ms=o.duration_ms)
        sides[setup.side] = outcomes
    return Analysis(base=sides["base"], head=sides["head"])
