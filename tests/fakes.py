"""Test doubles shared across test files."""

from core.sandbox.base import ExecResult


class FakeSandbox:
    """In-memory Sandbox: files live in a dict, command results are scripted by command prefix."""

    def __init__(self, results: dict[str, ExecResult] | None = None) -> None:
        self.results = results or {}  # "pytest" -> ExecResult, matched against the start of the command
        self.files: dict[str, bytes] = {}
        self.calls: list[tuple[list[str], str]] = []  # (cmd, cwd) in order
        self.network = True
        self.closed = False

    async def exec(self, cmd, cwd, timeout_s, env=None):
        self.calls.append((cmd, cwd))
        joined = " ".join(cmd)
        for prefix, result in self.results.items():
            if joined.startswith(prefix):
                return result
        return ExecResult(exit_code=0, stdout="", stderr="", duration_ms=1)

    async def upload_tree(self, tar_gz, dest, strip_components=0):
        self.files[f"{dest}/.uploaded"] = tar_gz

    async def read_file(self, path):
        return self.files[path]

    async def write_file(self, path, data):
        self.files[path] = data

    async def set_network(self, enabled):
        self.network = enabled

    async def close(self):
        self.closed = True
