from dataclasses import dataclass
from typing import Protocol

# Sandbox commands run as an unprivileged user, so the workspace lives in its home directory
# (the LLD's /work paths need root). Step 18 creates base/, head/, scratch/ and out/ under it.
WORK_ROOT = "/home/user/work"

# The ONLY environment variables PR code ever sees (LLD §9.4): no secrets, no worker config
SANDBOX_ENV = {"CI": "true", "PYTHONDONTWRITEBYTECODE": "1", "NODE_ENV": "test"}

# Per-stream cap on captured output (LLD §9.4); a runaway test can't flood the worker's memory
MAX_OUTPUT_CHARS = 1_000_000


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class Sandbox(Protocol):
    """Isolated machine for running untrusted PR code (LLD §9.1).

    Differs from the LLD in one way: network is switched for the whole sandbox with
    set_network(), not per exec, because that is how E2B works (verified in Step 16).
    """

    async def exec(
        self, cmd: list[str], cwd: str, timeout_s: int, env: dict[str, str] | None = None
    ) -> ExecResult: ...

    async def upload_tree(self, tar_gz: bytes, dest: str, strip_components: int = 0) -> None: ...

    async def read_file(self, path: str) -> bytes: ...

    async def write_file(self, path: str, data: bytes) -> None: ...

    async def set_network(self, enabled: bool) -> None: ...

    async def close(self) -> None: ...


def truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated {len(text) - limit} chars]"
