from xml.etree.ElementTree import ParseError

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring

from core.analysis.config import RepoConfig
from core.analysis.models import TestResult, ToolOutcome
from core.analysis.workspace import SideSetup
from core.sandbox.base import WORK_ROOT, Sandbox

OUT_DIR = f"{WORK_ROOT}/out"
MAX_REPORT_BYTES = 5_000_000
MAX_MESSAGE_CHARS = 4_000  # per test; the context builder trims further (LLD §11.2: 60 lines)

# pytest exit codes: 0 all passed, 1 some failed, 2 interrupted/collection errors, 5 no tests.
# 3 (internal error) and 4 (usage error) mean we have no trustworthy results.
USABLE_EXIT_CODES = {0, 1, 2, 5}


def _test_id(file: str, classname: str, name: str) -> str:
    module = file.removesuffix(".py").replace("/", ".")
    if classname.startswith(module + "."):  # e.g. "tests.test_x.TestGroup" → "TestGroup"
        return f"{file}::{classname[len(module) + 1:].replace('.', '::')}::{name}"
    return f"{file}::{name}"


def parse_junit(xml_text: str) -> list[TestResult]:
    """Parse pytest's JUnit XML (junit_family=xunit1, which includes the file path).

    The report is written inside the sandbox where PR code runs, so it is untrusted input:
    defusedxml refuses entity tricks (billion laughs, external entities). Raises ValueError if unusable.
    """
    try:
        root = fromstring(xml_text)
    except (ParseError, DefusedXmlException) as e:
        raise ValueError(f"bad JUnit XML: {e}") from e

    results = []
    for case in root.iter("testcase"):
        file = case.get("file") or ""
        classname = case.get("classname") or ""
        name = case.get("name") or ""
        outcome, message = "passed", None
        for tag in ("failure", "error", "skipped"):
            child = case.find(tag)
            if child is not None:
                outcome = {"failure": "failed", "error": "error", "skipped": "skipped"}[tag]
                message = "\n".join(p for p in (child.get("message"), child.text) if p)[:MAX_MESSAGE_CHARS]
                break
        if outcome == "error" and not classname:
            # pytest reports an import failure of a whole test file as one case with an empty classname
            test_id = f"{file or name}::<collection>"
        else:
            test_id = _test_id(file, classname, name)
        results.append(TestResult(test_id=test_id, outcome=outcome, message=message))
    return results


async def run_pytest(
    sbx: Sandbox, side: SideSetup, config: RepoConfig, env: dict[str, str] | None = None,
    targets: list[str] | None = None, timeout_s: int | None = None,
) -> ToolOutcome:
    """Run the whole suite, or only `targets` (e.g. one proof test file)."""
    report = f"{OUT_DIR}/{side.side}-junit.xml"
    timeout = timeout_s or config.test_timeout_s
    await sbx.exec(["mkdir", "-p", OUT_DIR], cwd="/", timeout_s=30)
    await sbx.exec(["rm", "-f", report], cwd="/", timeout_s=30)  # never read a stale report
    # TODO: honour config.test (custom test commands) — they can't be relied on to write JUnit
    result = await sbx.exec([
        side.python, "-m", "pytest", "-q", "-p", "no:cacheprovider",
        "--continue-on-collection-errors",  # one broken test file must not hide every other result
        f"--junitxml={report}", "-o", "junit_family=xunit1", *(targets or []),
    ], cwd=side.root, timeout_s=timeout, env=env)

    def outcome(available: bool, tests: list[TestResult] | None = None, log: str = "") -> ToolOutcome:
        return ToolOutcome("pytest", side.side, result.exit_code, result.duration_ms, result.timed_out,
                           available, [], tests or [], log)

    if result.timed_out:
        return outcome(False, log=f"tests timed out after {timeout}s")
    if result.exit_code not in USABLE_EXIT_CODES:
        return outcome(False, log=f"pytest exit {result.exit_code}: {(result.stderr or result.stdout)[-1000:]}")
    if result.exit_code == 5:
        return outcome(True, log="no tests collected")
    try:
        raw = await sbx.read_file(report)
    except Exception as e:  # missing report: pytest crashed before writing it
        return outcome(False, log=f"no JUnit report: {e}")
    if len(raw) > MAX_REPORT_BYTES:
        return outcome(False, log=f"JUnit report too large ({len(raw)} bytes)")
    try:
        return outcome(True, parse_junit(raw.decode("utf-8", errors="replace")))
    except ValueError as e:
        return outcome(False, log=str(e))
