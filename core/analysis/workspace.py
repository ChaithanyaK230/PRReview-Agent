import time
from dataclasses import dataclass

import structlog

from core.analysis.config import RepoConfig
from core.analysis.detect import TOOL_PACKAGES, TOOLS_DIR, Toolchain, detect, venv_python
from core.sandbox.base import WORK_ROOT, Sandbox

log = structlog.get_logger()

# Whole install phase (tools + both sides). Past it, tests are skipped and the review
# falls back to lint/diff only (LLD §17).
INSTALL_BUDGET_S = 240
SIDES = ("base", "head")


@dataclass(frozen=True)
class SideSetup:
    side: str
    root: str
    toolchain: Toolchain
    installed: bool
    log: str  # tail of install output, for the trace viewer
    duration_ms: int

    @property
    def python(self) -> str:
        return venv_python(self.root)


@dataclass(frozen=True)
class Workspace:
    base: SideSetup
    head: SideSetup
    tools_ok: bool

    @property
    def can_run_tests(self) -> bool:
        return self.tools_ok and self.base.installed and self.head.installed


def side_root(side: str) -> str:
    return f"{WORK_ROOT}/{side}"


class _Deadline:
    def __init__(self, seconds: int) -> None:
        self.end = time.monotonic() + seconds

    def remaining(self) -> int:
        return int(self.end - time.monotonic())


async def _run_steps(sbx: Sandbox, steps: list[list[str]], cwd: str, deadline: _Deadline) -> tuple[bool, str]:
    """Run commands in order, stopping at the first failure or when the budget runs out."""
    for cmd in steps:
        budget = deadline.remaining()
        if budget <= 0:
            return False, "install budget exhausted"
        result = await sbx.exec(cmd, cwd=cwd, timeout_s=budget)
        if not result.ok:
            reason = "timed out" if result.timed_out else f"exit {result.exit_code}"
            return False, f"`{' '.join(cmd)}` failed ({reason}):\n{(result.stderr or result.stdout)[-2000:]}"
    return True, ""


async def _root_files(sbx: Sandbox, root: str) -> set[str]:
    result = await sbx.exec(["find", root, "-maxdepth", "1", "-type", "f", "-printf", "%f\n"], cwd="/", timeout_s=30)
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


async def _setup_side(
    sbx: Sandbox, side: str, config: RepoConfig, tools_ok: bool, deadline: _Deadline
) -> SideSetup:
    root = side_root(side)
    started = time.perf_counter()
    toolchain = detect(await _root_files(sbx, root), root, config)
    if not toolchain.supported:
        installed, output = False, "unsupported language: analysis skipped"
    elif not tools_ok:
        installed, output = False, "tools failed to install"
    else:
        installed, output = await _run_steps(sbx, toolchain.install_steps, root, deadline)
    ms = int((time.perf_counter() - started) * 1000)
    log.info("workspace.side", side=side, strategy=toolchain.strategy, installed=installed, ms=ms)
    return SideSetup(side, root, toolchain, installed, output, ms)


async def prepare_workspace(sbx: Sandbox, base_tar: bytes, head_tar: bytes, config: RepoConfig) -> Workspace:
    """Unpack base and head, install their dependencies online, then cut the internet (LLD §9.2).

    Install failures are recorded, not raised: the review continues with less evidence.
    """
    deadline = _Deadline(INSTALL_BUDGET_S)
    await sbx.set_network(True)
    try:
        for side, tar in zip(SIDES, (base_tar, head_tar)):
            await sbx.upload_tree(tar, side_root(side), strip_components=1)  # GitHub tarballs have one top folder
        tools_ok, tools_log = await _run_steps(sbx, [
            ["python3", "-m", "venv", TOOLS_DIR],
            [f"{TOOLS_DIR}/bin/pip", "install", "-q", *TOOL_PACKAGES],
        ], WORK_ROOT, deadline)
        if not tools_ok:
            log.warning("workspace.tools_failed", log=tools_log[-500:])
        base = await _setup_side(sbx, "base", config, tools_ok, deadline)
        head = await _setup_side(sbx, "head", config, tools_ok, deadline)
    finally:
        # Whatever happened above, PR code never runs with internet access
        await sbx.set_network(False)
    return Workspace(base=base, head=head, tools_ok=tools_ok)
