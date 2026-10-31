"""Trace API (LLD §16) against the real test DB: shapes, iteration grouping, demo-repo visibility."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from api.deps import get_queue, get_session
from api.main import create_app
from core.db.models import (
    CandidateComment,
    CandidateStatus,
    CriticResult,
    Finding,
    Installation,
    JobState,
    JobTrigger,
    LLMCall,
    LLMPurpose,
    PostedComment,
    Repo,
    ReviewJob,
    ToolRun,
    ToolSide,
)
from core.settings import get_settings

DEMO = "ChaithanyaK230/prreview-demo"
PRIVATE = "someone/private-repo"


def comment(line, claim="add() subtracts", patch=None):
    return {"path": "calc.py", "line": line, "category": "bug", "severity": "high", "claim": claim,
            "body": "b", "confidence": 0.9, "suggested_patch": patch}


class PingQueue:
    async def ping(self):
        return True


@pytest.fixture
async def client(session, monkeypatch):
    monkeypatch.setattr(get_settings(), "demo_repos", [DEMO])
    app = create_app()

    async def _test_session():
        yield session

    app.dependency_overrides[get_session] = _test_session
    app.dependency_overrides[get_queue] = lambda: PingQueue()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def seeded(session):
    """One reviewed PR on the demo repo (a verified bug revised once, plus a dropped draft) and one private job."""
    session.add(Installation(id=1, account_login="ChaithanyaK230", account_type="User"))
    session.add_all([Repo(id=10, installation_id=1, full_name=DEMO), Repo(id=11, installation_id=1, full_name=PRIVATE)])
    await session.flush()
    t0 = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    job = ReviewJob(repo_id=10, pr_number=7, base_sha="a" * 40, head_sha="b" * 40, state=JobState.DONE,
                    trigger=JobTrigger.OPENED, cost_usd=Decimal("0.0043"), created_at=t0,
                    finished_at=t0 + timedelta(seconds=120))
    hidden = ReviewJob(repo_id=11, pr_number=1, base_sha="c" * 40, head_sha="d" * 40, state=JobState.DONE,
                       trigger=JobTrigger.OPENED)
    session.add_all([job, hidden])
    await session.flush()

    verified = CandidateComment(
        job_id=job.id, payload=comment(4, patch="@@ -4 +4 @@\n-a\n+b\n"), history=[comment(40)], iterations=2,
        final_status=CandidateStatus.VERIFIED, verified=True, patch_verified=True,
        proof_test="def test_add():\n    assert add(1, 2) == 3\n", score=5.9)
    dropped = CandidateComment(job_id=job.id, payload=comment(9, claim="style nit"), iterations=1,
                               final_status=CandidateStatus.DROPPED)
    session.add_all([verified, dropped])
    await session.flush()
    session.add_all([
        # inserted out of pipeline order on purpose: the API must sort them
        CriticResult(candidate_id=verified.id, iteration=1, critic="bug_proof", passed=True),
        CriticResult(candidate_id=verified.id, iteration=0, critic="anchor", passed=False, feedback="line 40 not in diff"),
        CriticResult(candidate_id=verified.id, iteration=1, critic="anchor", passed=True),
        CriticResult(candidate_id=verified.id, iteration=1, critic="patch_apply", passed=True),
        CriticResult(candidate_id=dropped.id, iteration=0, critic="anchor", passed=False, feedback="nope"),
        PostedComment(candidate_id=verified.id, github_comment_id=99, review_id=5, commit_sha="b" * 40, claim_hash="h"),
        LLMCall(job_id=job.id, purpose=LLMPurpose.DRAFT, model="deepseek/deepseek-chat", input_tokens=1000,
                output_tokens=200, cost_usd=Decimal("0.002")),
    ])
    run = ToolRun(job_id=job.id, tool="pytest", side=ToolSide.HEAD, exit_code=1, duration_ms=1500)
    session.add(run)
    await session.flush()
    session.add(Finding(tool_run_id=run.id, fingerprint="f", rule="failure_changed", path="tests/test_calc.py::test_add",
                        line=0, severity="error", message="assert -1 == 3", is_new=True))
    await session.commit()
    return job, hidden


async def test_lists_only_demo_repo_jobs(client, seeded):
    job, _ = seeded
    jobs = (await client.get("/api/jobs")).json()
    assert [j["id"] for j in jobs] == [str(job.id)]
    assert jobs[0] | {"id": None, "created_at": None, "finished_at": None} == {
        "id": None, "repo": DEMO, "pr": 7, "head_sha": "b" * 40, "state": "DONE", "cost_usd": 0.0043,
        "posted": 1, "verified": 1, "error": None, "created_at": None, "finished_at": None, "duration_s": 120.0,
    }
    assert (await client.get("/api/jobs", params={"pr": 8})).json() == []


async def test_private_job_is_404(client, seeded):
    _, hidden = seeded
    assert (await client.get(f"/api/jobs/{hidden.id}")).status_code == 404
    assert (await client.get(f"/api/jobs/{hidden.id}/candidates")).status_code == 404
    assert (await client.get(f"/api/jobs/{uuid.uuid4()}")).status_code == 404


async def test_job_detail_has_evidence_and_cost(client, seeded):
    job, _ = seeded
    d = (await client.get(f"/api/jobs/{job.id}")).json()
    assert d["tool_runs"] == [{"tool": "pytest", "side": "head", "exit_code": 1, "duration_ms": 1500,
                               "timed_out": False, "available": True}]
    assert d["findings"][0]["rule"] == "failure_changed"
    assert d["llm_calls"][0]["purpose"] == "draft" and d["llm_calls"][0]["cost_usd"] == 0.002


async def test_candidates_group_critics_by_iteration(client, seeded):
    job, _ = seeded
    cands = (await client.get(f"/api/jobs/{job.id}/candidates")).json()
    assert [c["final_status"] for c in cands] == ["verified", "dropped"]  # best score first
    v = cands[0]
    assert v["posted"] and v["patch_verified"] and v["proof_test"].startswith("def test_add")
    # version k is what the critics saw in round k: the first draft on line 40, the revision on line 4
    assert [(it["n"], it["comment"]["line"]) for it in v["iterations"]] == [(0, 40), (1, 4)]
    assert [c["critic"] for c in v["iterations"][0]["critics"]] == ["anchor"]
    assert [c["critic"] for c in v["iterations"][1]["critics"]] == ["anchor", "patch_apply", "bug_proof"]
    assert not cands[1]["posted"]


async def test_stats_cover_demo_repos(client, seeded):
    s = (await client.get("/api/stats")).json()
    assert (s["jobs"], s["comments_posted"], s["verified"], s["verified_rate"]) == (1, 1, 1, 1.0)
    assert s["mean_cost_usd"] == 0.0043 and s["p50_s"] == 120.0


async def test_dashboard_and_readyz(client, seeded):
    page = await client.get("/")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert (await client.get("/readyz")).json() == {"status": "ready"}
