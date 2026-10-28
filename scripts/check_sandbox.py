"""Smoke test for E2B: lifecycle, commands, files, and switching internet off on a running sandbox."""

import asyncio
import sys
import time

from e2b import ALL_TRAFFIC, AsyncSandbox, CommandExitException, TimeoutException

from core.settings import get_settings

SANDBOX_LIFETIME_S = 300  # hard cap; E2B destroys the sandbox after this even if we crash
COMMAND_TIMEOUT_S = 30
PYPI_CHECK = "curl -sS -m 5 -o /dev/null -w '%{http_code}' https://pypi.org"


async def run(sbx: AsyncSandbox, cmd: str) -> tuple[int, str, int]:
    """Run a command and return (exit_code, output, duration_ms). A non-zero exit is a result, not a crash."""
    started = time.perf_counter()
    try:
        result = await sbx.commands.run(cmd, timeout=COMMAND_TIMEOUT_S)
        code, out = result.exit_code, result.stdout
    except CommandExitException as e:  # the SDK raises on non-zero exit; for us that's normal (failing tests)
        code, out = e.exit_code, e.stdout or e.stderr
    except TimeoutException:
        code, out = -1, "timed out"
    return code, out.strip(), int((time.perf_counter() - started) * 1000)


async def has_internet(sbx: AsyncSandbox) -> bool:
    code, out, _ = await run(sbx, PYPI_CHECK)
    return code == 0 and out == "200"


async def main() -> None:
    key = get_settings().e2b_api_key
    if key is None:
        sys.exit("E2B_API_KEY is not set in .env")

    started = time.perf_counter()
    sbx = await AsyncSandbox.create(api_key=key.get_secret_value(), timeout=SANDBOX_LIFETIME_S)
    print(f"sandbox created in {int((time.perf_counter() - started) * 1000)} ms  (id: {sbx.sandbox_id})")
    try:
        for cmd in ("python3 --version", "uname -sr"):
            code, out, ms = await run(sbx, cmd)
            print(f"{cmd:<18} -> exit {code}, {ms} ms   {out}")

        await sbx.files.write("/tmp/work/hello.txt", "hello from prreview")
        print(f"file round-trip    -> {await sbx.files.read('/tmp/work/hello.txt')}")

        print(f"internet (start)   -> {'yes' if await has_internet(sbx) else 'no'}")

        # The design question: can we install with internet, then cut it before running PR code?
        await sbx.update_network({"deny_out": [ALL_TRAFFIC]})
        print(f"internet (after deny_out) -> {'yes  (NOT isolated!)' if await has_internet(sbx) else 'no  (isolated)'}")
    finally:
        await sbx.kill()  # always: a live sandbox bills per second
        print("sandbox killed")


if __name__ == "__main__":
    asyncio.run(main())
