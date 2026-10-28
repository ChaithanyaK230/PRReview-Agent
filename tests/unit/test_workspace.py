import pytest

from core.analysis.config import RepoConfig
from core.analysis.workspace import prepare_workspace
from core.sandbox.base import WORK_ROOT, ExecResult
from tests.fakes import FakeSandbox


def ok(stdout=""):
    return ExecResult(exit_code=0, stdout=stdout, stderr="", duration_ms=1)


def failed(stderr="boom"):
    return ExecResult(exit_code=1, stdout="", stderr=stderr, duration_ms=1)


def listing(base="calculator.py\n", head="calculator.py\n"):
    return {
        f"find {WORK_ROOT}/base": ok(base),
        f"find {WORK_ROOT}/head": ok(head),
    }


class NetworkTracker(FakeSandbox):
    """Records whether internet was on when each command ran."""

    def __init__(self, results=None):
        super().__init__(results)
        self.network_during = []

    async def exec(self, cmd, cwd, timeout_s, env=None):
        self.network_during.append((" ".join(cmd), self.network))
        return await super().exec(cmd, cwd, timeout_s, env)


async def test_happy_path_installs_both_sides_then_goes_offline():
    sbx = NetworkTracker(listing(head="requirements.txt\ncalculator.py\n"))
    ws = await prepare_workspace(sbx, b"base-tar", b"head-tar", RepoConfig())

    assert ws.can_run_tests
    assert (ws.base.toolchain.strategy, ws.head.toolchain.strategy) == ("none", "requirements")
    assert sbx.files[f"{WORK_ROOT}/base/.uploaded"] == b"base-tar"
    assert sbx.files[f"{WORK_ROOT}/head/.uploaded"] == b"head-tar"
    assert all(online for _, online in sbx.network_during)  # installs ran with internet
    assert sbx.network is False                             # ...and it was cut afterwards


async def test_failed_install_is_recorded_not_raised():
    sbx = FakeSandbox({**listing(head="requirements.txt\n"),
                       f"{WORK_ROOT}/tools/bin/uv pip install": failed("No matching distribution")})
    ws = await prepare_workspace(sbx, b"b", b"h", RepoConfig())
    assert not ws.head.installed and not ws.can_run_tests
    assert "No matching distribution" in ws.head.log
    assert sbx.network is False


async def test_tools_failure_skips_both_sides():
    sbx = FakeSandbox({**listing(), "python3 -m venv": failed("venv module missing")})
    ws = await prepare_workspace(sbx, b"b", b"h", RepoConfig())
    assert not ws.tools_ok
    assert ws.base.log == ws.head.log == "tools failed to install"


async def test_internet_is_cut_even_when_upload_crashes():
    class Exploding(FakeSandbox):
        async def upload_tree(self, tar_gz, dest, strip_components=0):
            raise RuntimeError("disk full")

    sbx = Exploding()
    with pytest.raises(RuntimeError):
        await prepare_workspace(sbx, b"b", b"h", RepoConfig())
    assert sbx.network is False


async def test_unsupported_language_is_skipped():
    sbx = FakeSandbox(listing(base="package.json\n", head="package.json\n"))
    ws = await prepare_workspace(sbx, b"b", b"h", RepoConfig())
    assert not ws.head.installed and ws.head.toolchain.strategy == "skipped"
