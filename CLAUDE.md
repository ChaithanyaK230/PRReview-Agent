# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project state

This repo is a **pre-implementation scaffold** for a PR Review Agent (a GitHub App that reviews pull requests). The only code is a `uv init` stub (`main.py`, `pyproject.toml` with no dependencies). The design is fully specified in two documents, which are the source of truth for anything you build:

- [HLD-pr-review-agent.md](HLD-pr-review-agent.md): goals, architecture, security model, cost and latency budgets, delivery phases.
- [LLD-pr-review-agent.md](LLD-pr-review-agent.md): the planned repo layout, DB schema, state machine, algorithms (delta, ranking, reflection loop), config variables, limits and testing strategy.

Read the relevant LLD section before implementing a module. When code and the LLD disagree, raise it rather than silently diverging.

## Commands

Python 3.12, managed with `uv` (no lockfile yet).

```bash
uv sync                 # create/update .venv
uv add <pkg>            # add a dependency (writes pyproject.toml + uv.lock)
uv run python main.py   # run the current stub
```

Planned entry points per the LLD (not yet present):

```bash
uv run uvicorn api.main:app --reload       # API service (webhook ingress + trace API)
uv run arq worker.main.WorkerSettings      # worker service
uv run alembic upgrade head                # migrations
uv run pytest tests/unit                   # unit tests
uv run pytest tests/path/test_x.py::test_y # single test
uv run pytest -m sandbox                   # real E2B sandbox tests (nightly)
uv run python evals/run_evals.py           # precision/recall evals on the seeded-bug dataset
```

## Architecture (big picture)

Pipeline: GitHub webhook → FastAPI ingress (verify HMAC, dedupe on `X-GitHub-Delivery`, enqueue, return 202) → Arq worker runs a **persisted state machine** per `(repo, PR, head_sha)`:

`QUEUED → FETCHING → SANDBOX_READY → ANALYZED → DRAFTED → REFLECTING → PUBLISHING → DONE` (plus `FAILED` / `CANCELLED`).

1. **Fetch** the PR, diff and `.prreview.yml` (always from the **base** branch, never head).
2. **Sandbox** (E2B): upload base and head tarballs, install deps with network on, then turn the network **off**.
3. **Analyze**: run lint, types, Semgrep and tests on **both base and head**, then compute the delta so only findings and failures this PR introduced are attributed to it.
4. **Draft**: Claude emits schema-validated `CandidateComment` JSON, one concurrent call per category group.
5. **Reflect**: ordered deterministic critics (anchor → grounding → duplicate_finding → patch_apply → patch_lint → patch_tests → bug_proof) short-circuit on the first failure. Failures go to the reviser LLM. After at most 3 iterations the comment is dropped. A bug claim whose proof test fails on head and passes with the patch becomes **Verified**.
6. **Rank and publish**: dedupe by proximity and `claim_hash`, cap at 7, then post one `COMMENT` review, upsert a summary comment (`<!-- prreview:summary -->` marker) and complete the Check Run.

Planned layout: `api/` and `worker/` hold wiring only; all logic lives in `core/` (`github`, `sandbox`, `analysis`, `context`, `agent`, `critics`, `ranking`, `db`). `core/` never imports from `api/` or `worker/`. Only `core/db` imports SQLAlchemy models.

## Invariants to preserve

- **The LLM proposes; deterministic code decides.** LLM output is always validated against Pydantic schemas. No LLM tool can call GitHub.
- **Untrusted input:** PR code, PR text and head-branch config. Wrap untrusted text in delimiters such as `<untrusted_pr_body>`. No secrets and no worker env vars enter the sandbox (only `CI`, `PYTHONDONTWRITEBYTECODE`, `NODE_ENV`).
- **Never block merges or push code:** reviews use `event: COMMENT`, Check Run conclusions are `success`/`neutral` (never `failure`), and the app has no Contents write permission.
- **Resumable and idempotent:** each step persists its outputs and the state transition in one transaction (`repo.advance`). Before each step except `PUBLISHING`, check Redis `pr:{repo_id}:{pr}:latest` and cancel if a newer push superseded the job. `PUBLISHING` cannot be cancelled.
- **Precision over recall:** when in doubt, drop the comment.
- **Error classes** (`core/errors.py`): `TransientError` (retry with backoff), `FatalError` (job FAILED, neutral Check Run), `CancelledJob`, `BudgetExceeded` (publish what's accepted so far). `CostMeter` caps LLM spend at $0.50 per job.
- **Migrations** must be backward compatible (expand, then contract) so old workers can finish in-flight jobs.
- **Evals gate:** changes to prompts or critics must run the eval suite. CI fails if precision drops more than 5 points from the baseline.

## Stack

FastAPI, Arq/Redis, SQLAlchemy 2.0 async + Alembic (PostgreSQL), Pydantic v2 / pydantic-settings, githubkit + PyJWT, unidiff, py-tree-sitter, E2B, Anthropic SDK, Langfuse + OpenTelemetry, and a Next.js dashboard in `web/`. Deployed to Fly.io as two process groups, `api` and `worker`. Tests use pytest, pytest-asyncio, respx, hypothesis and testcontainers.
