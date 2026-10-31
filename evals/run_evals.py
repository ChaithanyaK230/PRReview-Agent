"""Run the seeded-bug evals through the real review pipeline and report precision/recall (LLD §19.1).

Usage:
    uv run python -m evals.run_evals                     # all cases
    uv run python -m evals.run_evals --cases sql-injection,clean-refactor
    uv run python -m evals.run_evals --update-baseline   # save this run as the new baseline
    uv run python -m evals.run_evals --check             # exit 1 if precision fell > 5 points (CI gate)

Costs real money: one LLM review (~$0.002-0.01) and one E2B sandbox per case.
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import structlog
from openai import AsyncOpenAI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from core.agent.llm import LLM
from core.db import repo
from core.db.models import Base, CandidateComment, CandidateStatus, JobTrigger, LLMCall, PostedComment
from core.sandbox.e2b import E2BSandbox
from core.settings import get_settings
from evals.harness import Case, CaseScore, EvalGitHub, Posted, load_cases, score_case
from worker.orchestrator import run_review

EVAL_DB_URL = os.environ.get("EVAL_DATABASE_URL", "postgresql+asyncpg://prreview:prreview@127.0.0.1:5434/prreview_eval")
RESULTS_DIR = Path(__file__).parent / "results"
BASELINE = Path(__file__).parent / "baseline.json"
MAX_PRECISION_DROP = 5.0  # percentage points (CLAUDE.md: evals gate)
# One at a time: each review already makes 3 concurrent LLM calls, and in parallel the first run
# hit DeepSeek's shared upstream rate limit (429) on OpenRouter
PARALLEL_CASES = 1


class LatestSha:
    """Stands in for Redis: the case's head is always the newest push, so nothing gets cancelled."""

    def __init__(self, sha: str) -> None:
        self.sha = sha

    async def get(self, key: str) -> bytes:
        return self.sha.encode()


@dataclass
class CaseRun:
    case_id: str
    state: str
    error: str | None
    posted: list[Posted]
    drafted: int
    dropped: int
    cost_usd: float
    seconds: float
    score: CaseScore = field(default_factory=CaseScore)


async def run_case(case: Case, index: int, sessionmaker, llm_client: AsyncOpenAI) -> CaseRun:
    settings = get_settings()
    repo_id = 9000 + index  # one repo per case, so cross-push dedupe never leaks between cases
    async with sessionmaker.begin() as s:
        await repo.upsert_installation(s, 1, "evals", "User")
        await repo.upsert_repo(s, repo_id, 1, f"evals/{case.id}", private=False)
        job, _ = await repo.create_job(s, repo_id, 1, case.base_sha, case.head_sha, JobTrigger.OPENED)
        job_id = job.id

    gh = EvalGitHub(case, settings.bot_login)
    ctx = {"sessionmaker": sessionmaker, "redis": LatestSha(case.head_sha), "gh_for": lambda _: gh,
           "llm_for": lambda jid: LLM(llm_client, settings.llm_model, jid)}
    if settings.e2b_api_key is not None:
        key = settings.e2b_api_key.get_secret_value()
        ctx["sandbox_factory"] = lambda: E2BSandbox.create(key)

    started, error = time.perf_counter(), None
    try:
        await run_review(ctx, str(job_id))
    except Exception as e:  # report it as a failed case, don't abort the whole eval run
        error = f"{type(e).__name__}: {e}"
    seconds = time.perf_counter() - started

    async with sessionmaker() as s:
        job = await repo.get_job(s, job_id)
        rows = (await s.execute(
            select(CandidateComment.payload, CandidateComment.verified)
            .join(PostedComment, PostedComment.candidate_id == CandidateComment.id)
            .where(CandidateComment.job_id == job_id)
        )).all()
        candidates = await repo.get_candidates(s, job_id)
        cost = await s.scalar(select(func.coalesce(func.sum(LLMCall.cost_usd), 0)).where(LLMCall.job_id == job_id))
    posted = [Posted(p["path"], p["line"], p["category"], verified) for p, verified in rows]
    run = CaseRun(case.id, str(job.state), error or job.error, posted, len(candidates),
                  sum(c.final_status == CandidateStatus.DROPPED for c in candidates), float(cost), seconds)
    run.score = score_case(case.expected, posted)
    return run


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))] if ordered else 0.0


def summarize(runs: list[CaseRun]) -> dict:
    tp = sum(r.score.true_positives for r in runs)
    posted = sum(r.score.true_positives + r.score.false_positives for r in runs)
    found = sum(r.score.found for r in runs)
    expected = sum(r.score.expected for r in runs)
    bug_posts = [p for r in runs for p in r.posted if p.category == "bug"]
    times = [r.seconds for r in runs]
    return {
        "precision": round(100 * tp / posted, 1) if posted else None,
        "recall": round(100 * found / expected, 1) if expected else None,
        "verified_rate": round(100 * sum(p.verified for p in bug_posts) / len(bug_posts), 1) if bug_posts else None,
        "posted": posted, "true_positives": tp, "found": found, "expected": expected,
        "mean_cost_usd": round(sum(r.cost_usd for r in runs) / len(runs), 5) if runs else 0,
        "p50_seconds": round(percentile(times, 0.5), 1), "p95_seconds": round(percentile(times, 0.95), 1),
        "failed_cases": [r.case_id for r in runs if r.state != "DONE"],
    }


def print_report(runs: list[CaseRun], summary: dict) -> None:
    print(f"\n{'case':<24}{'state':<8}{'exp':>4}{'post':>5}{'TP':>4}{'FP':>4}{'found':>6}{'verif':>6}"
          f"{'drafts':>7}{'cost $':>9}{'secs':>6}")
    for r in runs:
        print(f"{r.case_id:<24}{r.state:<8}{r.score.expected:>4}{len(r.posted):>5}{r.score.true_positives:>4}"
              f"{r.score.false_positives:>4}{r.score.found:>6}{sum(p.verified for p in r.posted):>6}"
              f"{r.drafted:>7}{r.cost_usd:>9.4f}{r.seconds:>6.0f}")
        for p in r.score.unmatched_posts:
            print(f"    false positive: {p.path}:{p.line} ({p.category})")
        if r.error:
            print(f"    error: {r.error[:150]}")

    def pct(key: str) -> str:
        return "n/a" if summary[key] is None else f"{summary[key]}%"

    print(f"\nprecision {pct('precision')} ({summary['true_positives']}/{summary['posted']} posted were right)   "
          f"recall {pct('recall')} ({summary['found']}/{summary['expected']} seeded bugs found)   "
          f"verified {pct('verified_rate')} of bug comments")
    print(f"mean cost ${summary['mean_cost_usd']} per review   latency p50 {summary['p50_seconds']}s  "
          f"p95 {summary['p95_seconds']}s")
    print("targets (HLD 1.4): precision >= 80%, recall >= 50%, verified >= 40%, cost <= $0.30")


def compare_to_baseline(summary: dict) -> bool:
    if not BASELINE.exists() or summary["precision"] is None:
        print("no baseline to compare against" if not BASELINE.exists() else "nothing posted: precision undefined")
        return True
    baseline = json.loads(BASELINE.read_text())["summary"]
    drop = (baseline["precision"] or 0) - summary["precision"]
    ok = drop <= MAX_PRECISION_DROP
    print(f"baseline precision {baseline['precision']}% -> now {summary['precision']}% "
          f"({'OK' if ok else f'FAILED: dropped {drop:.1f} points, limit {MAX_PRECISION_DROP}'})")
    return ok


async def main(args: argparse.Namespace) -> int:
    if not args.verbose:  # the pipeline's per-step logs are noise here; keep warnings and errors
        structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))
    cases = load_cases()
    if args.cases:
        wanted = set(args.cases.split(","))
        cases = [c for c in cases if c.id in wanted]

    engine = create_async_engine(EVAL_DB_URL)
    async with engine.begin() as conn:  # fresh tables every run: results never mix between runs
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    s = get_settings()
    llm_client = AsyncOpenAI(base_url=s.llm_base_url, api_key=s.openrouter_api_key.get_secret_value(),
                             max_retries=0, timeout=120)

    gate = asyncio.Semaphore(PARALLEL_CASES)

    async def one(index: int, case: Case) -> CaseRun:
        async with gate:
            print(f"running {case.id} ...", flush=True)
            return await run_case(case, index, sessionmaker, llm_client)

    print(f"model {s.llm_model}, {len(cases)} cases, sandbox {'on' if s.e2b_api_key else 'OFF (diff-only)'}")
    runs = list(await asyncio.gather(*(one(i, c) for i, c in enumerate(cases))))
    await llm_client.close()
    await engine.dispose()

    summary = summarize(runs)
    print_report(runs, summary)
    RESULTS_DIR.mkdir(exist_ok=True)
    result = {"at": datetime.now(timezone.utc).isoformat(), "model": s.llm_model, "summary": summary,
              "cases": [{**asdict(r), "score": asdict(r.score)} for r in runs]}
    (RESULTS_DIR / "latest.json").write_text(json.dumps(result, indent=2, default=str))
    if args.update_baseline:
        BASELINE.write_text(json.dumps(result, indent=2, default=str))
        print(f"baseline updated: {BASELINE}")
    if args.check and not compare_to_baseline(summary):
        return 1
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cases", help="comma-separated case ids (default: all)")
    parser.add_argument("--update-baseline", action="store_true")
    parser.add_argument("--check", action="store_true", help="exit 1 if precision dropped more than 5 points")
    parser.add_argument("--verbose", action="store_true", help="show the pipeline's step logs")
    sys.exit(asyncio.run(main(parser.parse_args())))
