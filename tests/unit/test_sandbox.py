"""E2BSandbox logic, tested against a stub of the E2B SDK object (no network, no cost)."""

import pytest
from e2b import ALL_TRAFFIC, CommandExitException, SandboxException, TimeoutException
from e2b.sandbox.commands.command_handle import CommandResult

from core.errors import FatalError, TransientError
from core.sandbox.base import SANDBOX_ENV, truncate
from core.sandbox.e2b import E2BSandbox


class StubCommands:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)  # CommandResult to return, or an exception to raise, per call
        self.calls = []

    async def run(self, cmd, cwd=None, envs=None, timeout=None):
        self.calls.append({"cmd": cmd, "cwd": cwd, "envs": envs, "timeout": timeout})
        outcome = self.outcomes.pop(0) if self.outcomes else CommandResult(stderr="", stdout="", exit_code=0, error=None)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class StubFiles:
    def __init__(self):
        self.data = {}

    async def write(self, path, data):
        self.data[path] = data

    async def read(self, path, format="text"):
        return bytearray(self.data[path])


class StubSdk:
    sandbox_id = "sbx-test"

    def __init__(self, outcomes=()):
        self.commands = StubCommands(outcomes)
        self.files = StubFiles()
        self.network_updates = []
        self.kills = 0

    async def update_network(self, network):
        self.network_updates.append(network)

    async def kill(self):
        self.kills += 1


def ok(stdout=""):
    return CommandResult(stderr="", stdout=stdout, exit_code=0, error=None)


def failed(code, stdout="", stderr=""):
    return CommandExitException(stderr=stderr, stdout=stdout, exit_code=code, error=None)


async def test_success_is_returned_with_duration():
    sdk = StubSdk([ok("Python 3.11.6")])
    result = await E2BSandbox(sdk).exec(["python3", "--version"], cwd="/", timeout_s=10)
    assert result.ok and result.stdout == "Python 3.11.6" and result.duration_ms >= 0


async def test_nonzero_exit_is_a_result_not_an_exception():
    sdk = StubSdk([failed(1, stdout="1 failed, 3 passed")])
    result = await E2BSandbox(sdk).exec(["pytest"], cwd="/w", timeout_s=10)
    assert (result.exit_code, result.stdout, result.ok) == (1, "1 failed, 3 passed", False)


async def test_timeout_is_marked_not_raised():
    result = await E2BSandbox(StubSdk([TimeoutException("deadline")])).exec(["sleep", "999"], cwd="/", timeout_s=1)
    assert result.timed_out and not result.ok


async def test_broken_sandbox_is_transient():
    with pytest.raises(TransientError):
        await E2BSandbox(StubSdk([SandboxException("sandbox not found")])).exec(["ls"], cwd="/", timeout_s=1)


async def test_command_is_quoted_and_env_is_only_the_safe_set():
    sdk = StubSdk()
    await E2BSandbox(sdk).exec(["cat", "evil; rm -rf ~.txt"], cwd="/w", timeout_s=5)
    call = sdk.commands.calls[0]
    assert call["cmd"] == "cat 'evil; rm -rf ~.txt'"  # a hostile file name stays one argument
    assert call["envs"] == SANDBOX_ENV
    assert (call["cwd"], call["timeout"]) == ("/w", 5)


async def test_upload_unpacks_and_cleans_up():
    sdk = StubSdk()
    await E2BSandbox(sdk).upload_tree(b"tarbytes", "/home/user/work/head", strip_components=1)
    cmds = [c["cmd"] for c in sdk.commands.calls]
    assert cmds[0] == "mkdir -p /home/user/work/head"
    assert cmds[1].startswith("tar -xzf /tmp/upload-") and cmds[1].endswith("--strip-components=1")
    assert cmds[2].startswith("rm -f /tmp/upload-")
    assert list(sdk.files.data.values()) == [b"tarbytes"]


async def test_failed_unpack_is_fatal_and_still_cleans_up():
    sdk = StubSdk([ok(), failed(2, stderr="gzip: not in gzip format")])
    with pytest.raises(FatalError):
        await E2BSandbox(sdk).upload_tree(b"garbage", "/home/user/work/head")
    assert sdk.commands.calls[-1]["cmd"].startswith("rm -f /tmp/upload-")


async def test_network_switch_payloads():
    sdk = StubSdk()
    sbx = E2BSandbox(sdk)
    await sbx.set_network(False)
    await sbx.set_network(True)
    assert sdk.network_updates == [{"deny_out": [ALL_TRAFFIC]}, {}]


async def test_read_returns_bytes():
    sdk = StubSdk()
    sbx = E2BSandbox(sdk)
    await sbx.write_file("/tmp/a", b"hi")
    assert await sbx.read_file("/tmp/a") == b"hi"


async def test_close_is_idempotent_and_context_managed():
    sdk = StubSdk()
    async with E2BSandbox(sdk) as sbx:
        pass
    await sbx.close()
    assert sdk.kills == 1


def test_truncate():
    assert truncate("abc", limit=5) == "abc"
    assert truncate("abcdefgh", limit=3).startswith("abc\n... [truncated 5 chars]")
