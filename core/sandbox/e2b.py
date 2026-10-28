import shlex
import time
import uuid

import structlog
from e2b import (
    ALL_TRAFFIC, AsyncSandbox, AuthenticationException, CommandExitException, SandboxException,
    TimeoutException,
)

from core.errors import FatalError, TransientError
from core.sandbox.base import SANDBOX_ENV, ExecResult, truncate

log = structlog.get_logger()

DEFAULT_LIFETIME_S = 15 * 60  # hard cap: E2B destroys the sandbox even if our worker crashes (LLD §9.4)


class E2BSandbox:
    """Sandbox implementation backed by an E2B micro-VM."""

    def __init__(self, sbx: AsyncSandbox) -> None:
        self._sbx = sbx
        self._closed = False

    @property
    def sandbox_id(self) -> str:
        return self._sbx.sandbox_id

    @classmethod
    async def create(
        cls, api_key: str, lifetime_s: int = DEFAULT_LIFETIME_S, template: str | None = None
    ) -> "E2BSandbox":
        try:
            sbx = await AsyncSandbox.create(template=template, api_key=api_key, timeout=lifetime_s, envs=SANDBOX_ENV)
        except AuthenticationException as e:
            raise FatalError(f"E2B rejected the API key: {e}", user_message="The sandbox service is misconfigured.") from e
        except SandboxException as e:
            raise TransientError(f"sandbox create failed: {e}") from e
        log.info("sandbox.created", sandbox_id=sbx.sandbox_id, template=template or "base")
        return cls(sbx)

    async def exec(
        self, cmd: list[str], cwd: str, timeout_s: int, env: dict[str, str] | None = None
    ) -> ExecResult:
        """Run a command. A non-zero exit or a timeout is a normal result (failing tests), never an exception."""
        started = time.perf_counter()
        timed_out = False
        try:
            # cmd is a list and shlex.join quotes each part, so file names from a PR can't inject shell syntax
            result = await self._sbx.commands.run(
                shlex.join(cmd), cwd=cwd, envs={**SANDBOX_ENV, **(env or {})}, timeout=timeout_s,
            )
            code, out, err = result.exit_code, result.stdout, result.stderr
        except CommandExitException as e:  # E2B raises on non-zero exit
            code, out, err = e.exit_code, e.stdout, e.stderr
        except TimeoutException as e:
            code, out, err, timed_out = -1, "", f"timed out after {timeout_s}s: {e}", True
        except SandboxException as e:  # the sandbox itself is broken or gone, not the command
            raise TransientError(f"sandbox exec failed: {e}") from e
        return ExecResult(
            exit_code=code, stdout=truncate(out or ""), stderr=truncate(err or ""),
            duration_ms=int((time.perf_counter() - started) * 1000), timed_out=timed_out,
        )

    async def upload_tree(self, tar_gz: bytes, dest: str, strip_components: int = 0) -> None:
        """Upload a .tar.gz and unpack it into dest. GitHub tarballs need strip_components=1."""
        archive = f"/tmp/upload-{uuid.uuid4().hex}.tar.gz"  # unique, so parallel uploads can't collide
        await self.write_file(archive, tar_gz)
        unpack = await self.exec(["mkdir", "-p", dest], cwd="/", timeout_s=30)
        if unpack.ok:
            unpack = await self.exec(
                ["tar", "-xzf", archive, "-C", dest, f"--strip-components={strip_components}"], cwd="/", timeout_s=180,
            )
        await self.exec(["rm", "-f", archive], cwd="/", timeout_s=30)
        if not unpack.ok:
            raise FatalError(
                f"unpacking into {dest} failed: {unpack.stderr[:300]}",
                user_message="The repository could not be unpacked in the sandbox.",
            )

    async def read_file(self, path: str) -> bytes:
        return bytes(await self._sbx.files.read(path, format="bytes"))

    async def write_file(self, path: str, data: bytes) -> None:
        await self._sbx.files.write(path, data)

    async def set_network(self, enabled: bool) -> None:
        # update_network replaces ALL egress rules: {} = allow everything, deny ALL_TRAFFIC = fully offline
        await self._sbx.update_network({} if enabled else {"deny_out": [ALL_TRAFFIC]})
        log.info("sandbox.network", sandbox_id=self.sandbox_id, enabled=enabled)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._sbx.kill()
        except SandboxException as e:  # already gone; its lifetime cap cleans up anyway
            log.warning("sandbox.kill_failed", sandbox_id=self.sandbox_id, error=str(e))

    async def __aenter__(self) -> "E2BSandbox":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()
