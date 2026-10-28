"""Parsers and runners, using real ruff / pytest output captured on 2026-10-28."""

import json

import pytest

from core.analysis.config import RepoConfig
from core.analysis.detect import Toolchain
from core.analysis.runners.pytest import OUT_DIR, parse_junit, run_pytest
from core.analysis.runners.ruff import parse_ruff, run_ruff
from core.analysis.workspace import SideSetup
from core.sandbox.base import ExecResult
from tests.fakes import FakeSandbox

ROOT = "/home/user/work/head"
SIDE = SideSetup("head", ROOT, Toolchain("python", "none"), installed=True, log="", duration_ms=0)


def ruff_item(code, path, row, message, end_row=None):
    return {"cell": None, "code": code, "filename": f"{ROOT}/{path}", "fix": None,
            "location": {"column": 1, "row": row}, "end_location": {"column": 9, "row": end_row or row},
            "message": message, "name": "x", "noqa_row": row, "severity": "error", "url": None}


RUFF_JSON = json.dumps([
    ruff_item("F821", "pkg/bad.py", 5, "Undefined name `undefined_name`"),
    ruff_item("I001", "pkg/bad.py", 1, "Import block is un-sorted or un-formatted"),
    ruff_item("invalid-syntax", "syntax.py", 1, "Expected a parameter or the end of the parameter list"),
    ruff_item("F401", "migrations/0001_init.py", 1, "`os` imported but unused"),
])

JUNIT = """<?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests">
<testsuite name="pytest" errors="1" failures="1" skipped="1" tests="5">
<testcase classname="" name="test_broken" file="test_broken.py" time="0.000">
  <error message="collection failure">E   ModuleNotFoundError: No module named 'does_not_exist'</error></testcase>
<testcase classname="test_sample" name="test_ok" file="test_sample.py" line="2" time="0.001" />
<testcase classname="test_sample" name="test_fail" file="test_sample.py" line="5" time="0.001">
  <failure message="AssertionError: nope&#10;assert 1 == 2">E       AssertionError: nope</failure></testcase>
<testcase classname="test_sample" name="test_skip" file="test_sample.py" line="8" time="0.000">
  <skipped type="pytest.skip" message="later">skipped: later</skipped></testcase>
<testcase classname="tests.test_calc.TestGroup" name="test_in_class" file="tests/test_calc.py" line="13" />
</testsuite></testsuites>"""


# ---------- ruff ----------

def test_parse_ruff_makes_paths_relative_and_rates_severity():
    by_rule = {f.rule: f for f in parse_ruff(RUFF_JSON, ROOT)}
    assert by_rule["F821"].path == "pkg/bad.py" and by_rule["F821"].line == 5
    assert by_rule["F821"].severity == "error"           # undefined name = real breakage
    assert by_rule["I001"].severity == "warning"         # import order = style
    assert by_rule["invalid-syntax"].severity == "error"


def test_parse_ruff_rejects_garbage():
    with pytest.raises(ValueError):
        parse_ruff("Traceback (most recent call last)...", ROOT)


async def test_run_ruff_drops_ignored_paths():
    sbx = FakeSandbox({f"{'/home/user/work/tools/bin/ruff'}": ExecResult(0, RUFF_JSON, "", 120)})
    outcome = await run_ruff(sbx, SIDE, RepoConfig())
    assert outcome.available
    assert "migrations/0001_init.py" not in {f.path for f in outcome.findings}  # default ignore_paths
    assert len(outcome.findings) == 3


async def test_ruff_crash_means_no_evidence_not_no_problems():
    sbx = FakeSandbox({"/home/user/work/tools/bin/ruff": ExecResult(2, "", "error: invalid config", 50)})
    outcome = await run_ruff(sbx, SIDE, RepoConfig())
    assert not outcome.available and "invalid config" in outcome.log


# ---------- pytest ----------

def test_parse_junit_outcomes_and_ids():
    results = {t.test_id: t for t in parse_junit(JUNIT)}
    assert results["test_sample.py::test_ok"].outcome == "passed"
    assert results["test_sample.py::test_fail"].outcome == "failed"
    assert "AssertionError: nope" in results["test_sample.py::test_fail"].message
    assert results["test_sample.py::test_skip"].outcome == "skipped"
    assert results["tests/test_calc.py::TestGroup::test_in_class"].outcome == "passed"
    assert results["test_broken.py::<collection>"].outcome == "error"


def test_parse_junit_refuses_entity_bombs():
    bomb = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;">]><x>&b;</x>'
    with pytest.raises(ValueError):
        parse_junit(bomb)


def sandbox_with_report(exit_code, report=JUNIT):
    sbx = FakeSandbox({f"{SIDE.python} -m pytest": ExecResult(exit_code, "", "", 900)})
    if report is not None:
        sbx.files[f"{OUT_DIR}/head-junit.xml"] = report.encode()
    return sbx


async def test_run_pytest_reads_report_even_when_tests_fail():
    outcome = await run_pytest(sandbox_with_report(1), SIDE, RepoConfig())
    assert outcome.available and len(outcome.tests) == 5


async def test_run_pytest_continues_past_collection_errors():
    sbx = sandbox_with_report(1)
    await run_pytest(sbx, SIDE, RepoConfig())
    pytest_cmd = next(cmd for cmd, _ in sbx.calls if "-m" in cmd)
    assert "--continue-on-collection-errors" in pytest_cmd


@pytest.mark.parametrize("exit_code, report, available", [
    (5, None, True),      # no tests: valid evidence ("nothing to run")
    (3, JUNIT, False),    # pytest internal error
    (1, None, False),     # crashed before writing the report
])
async def test_run_pytest_edge_cases(exit_code, report, available):
    outcome = await run_pytest(sandbox_with_report(exit_code, report), SIDE, RepoConfig())
    assert outcome.available is available


async def test_run_pytest_timeout_is_no_evidence():
    sbx = FakeSandbox({f"{SIDE.python} -m pytest": ExecResult(-1, "", "", 360000, timed_out=True)})
    outcome = await run_pytest(sbx, SIDE, RepoConfig())
    assert not outcome.available and "timed out" in outcome.log
