import json

from core.analysis.config import RepoConfig
from core.analysis.detect import TOOLS_DIR
from core.analysis.models import Finding, ToolOutcome
from core.analysis.workspace import SideSetup
from core.sandbox.base import ExecResult, Sandbox

RUFF = f"{TOOLS_DIR}/bin/ruff"
TOOL_TIMEOUT_S = 360  # LLD §17: tools per side


def _severity(rule: str) -> str:
    # Pyflakes (F) and syntax errors are real breakage; style rules are warnings
    return "error" if rule.startswith(("F", "E9")) or rule == "invalid-syntax" else "warning"


def parse_ruff(output: str, root: str) -> list[Finding]:
    """Parse `ruff check --output-format json`. Raises ValueError if the output isn't ruff's JSON."""
    try:
        items = json.loads(output or "[]")
    except json.JSONDecodeError as e:
        raise ValueError(f"ruff output is not JSON: {e}") from e
    if not isinstance(items, list):
        raise ValueError("ruff output is not a JSON array")

    prefix = root.rstrip("/") + "/"
    findings = []
    for item in items:
        rule = item.get("code") or "invalid-syntax"
        end = item.get("end_location") or {}
        findings.append(Finding(
            tool="ruff",
            rule=rule,
            path=item["filename"].removeprefix(prefix),  # sandbox gives absolute paths
            line=item["location"]["row"],
            end_line=end.get("row"),
            severity=_severity(rule),
            message=item["message"],
        ))
    return findings


async def ruff_check(sbx: Sandbox, root: str, paths: list[str]) -> ExecResult:
    # --exit-zero: findings don't change the exit code, so non-zero always means ruff itself failed.
    # The repo's own ruff config (pyproject.toml / ruff.toml) is picked up automatically from cwd.
    return await sbx.exec([RUFF, "check", "--output-format", "json", "--exit-zero", "--no-cache", *paths],
                          cwd=root, timeout_s=TOOL_TIMEOUT_S)


async def run_ruff(sbx: Sandbox, side: SideSetup, config: RepoConfig) -> ToolOutcome:
    result = await ruff_check(sbx, side.root, ["."])

    def outcome(available: bool, findings: list[Finding] | None = None, log: str = "") -> ToolOutcome:
        return ToolOutcome("ruff", side.side, result.exit_code, result.duration_ms, result.timed_out,
                           available, findings or [], [], log)

    if result.timed_out or result.exit_code != 0:
        return outcome(False, log=(result.stderr or "timed out")[-1000:])
    try:
        findings = parse_ruff(result.stdout, side.root)
    except (ValueError, KeyError, TypeError) as e:
        return outcome(False, log=f"unparseable ruff output: {e}")
    return outcome(True, [f for f in findings if not config.is_ignored(f.path)])
