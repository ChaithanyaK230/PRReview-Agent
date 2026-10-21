import uuid

from core.db import repo
from core.db.models import CandidateStatus, CriticResult, JobState, JobTrigger, LLMCall
from core.errors import FatalError, TransientError
from core.github.client import PullRequest
from core.settings import get_settings
from worker import orchestrator
from worker.orchestrator import run_review

HEAD = "b" * 40
DIFF = "\n".join([
    "diff --git a/app.py b/app.py",
    "index 1111111..2222222 100644",
    "--- a/app.py",
    "+++ b/app.py",
    "@@ -1,1 +1,2 @@",
    " x = 1",
    "+y = 2",
]) + "\n"


class FakeGitHub:
    """Fake GitHub that records every write so tests can assert what the bot posted."""

    def __init__(self, head_sha=HEAD, transient_failures=0, fatal=False, existing_reviews=()):
        self.head_sha = head_sha
        self.transient_failures = transient_failures
        self.fatal = fatal
        self.pull_calls = 0
        self.reviews = list(existing_reviews)       # dicts as GitHub returns them
        self.review_comments: dict[int, list] = {}  # review id -> posted inline comments
        self.created_reviews = []                   # payloads passed to create_review
        self.issue_comments = []                    # (marker, body) passed to upsert
        self.check_runs = []                        # ("create", title) / ("complete", conclusion, title)

    async def list_reviews(self, repo_name, number):
        return self.reviews

    async def create_review(self, repo_name, number, commit_sha, body, comments):
        review_id = 900 + len(self.created_reviews)
        self.created_reviews.append({"commit_id": commit_sha, "body": body, "comments": comments})
        self.reviews.append({"id": review_id, "commit_id": commit_sha, "user": {"login": get_settings().bot_login}})
        self.review_comments[review_id] = [{"id": 5000 + i, "body": c["body"]} for i, c in enumerate(comments)]
        return review_id

    async def list_review_comments(self, repo_name, number, review_id):
        return self.review_comments.get(review_id, [])

    async def upsert_issue_comment(self, repo_name, number, marker, body, bot_login):
        self.issue_comments.append((marker, body))
        return 4242

    async def create_check_run(self, repo_name, head_sha, title):
        self.check_runs.append(("create", title))
        return 777

    async def complete_check_run(self, repo_name, check_run_id, conclusion, title, summary):
        self.check_runs.append(("complete", conclusion, title))

    async def get_pull(self, repo_name, number):
        self.pull_calls += 1
        if self.fatal:
            raise FatalError("PR not found")
        if self.pull_calls <= self.transient_failures:
            raise TransientError("GitHub 502")
        return PullRequest(number=number, title="t", body="", author="alice", draft=False, state="open",
                           base_ref="main", base_sha="a" * 40, head_sha=self.head_sha)

    async def get_diff(self, repo_name, number):
        return DIFF

    async def get_file(self, repo_name, path, ref):
        return None


class FakeLLM:
    """Answers the first draft call with `comments`, later ones with nothing; records one call each."""

    def __init__(self, comments=()):
        self.comments = list(comments)
        self.calls = 0
        self.records = []
        self.job_id = None

    async def structured(self, purpose, system, user, schema, meter, max_tokens=4000):
        self.calls += 1
        self.records.append(LLMCall(job_id=self.job_id, purpose=purpose, model="fake",
                                    input_tokens=10, output_tokens=5, cost_usd=0))
        return schema.model_validate({"comments": self.comments if self.calls == 1 else []})

    def drain_records(self):
        out, self.records = self.records, []
        return out


def comment(line, confidence=0.9):
    return {"path": "app.py", "line": line, "category": "bug", "severity": "medium",
            "claim": f"problem on line {line}", "body": "details", "confidence": confidence}


class FakeRedis:
    def __init__(self, latest=HEAD):
        self.latest = latest

    async def get(self, key):
        return self.latest.encode()


async def seed(session) -> str:
    async with session.begin():
        await repo.upsert_installation(session, 1, "octo", "User")
        await repo.upsert_repo(session, 10, 1, "octo/demo", private=False)
        job, _ = await repo.create_job(session, 10, 1, "a" * 40, HEAD, JobTrigger.OPENED)
    return str(job.id)


async def run(sessionmaker, gh, redis, job_id, llm=None):
    llm = llm or FakeLLM()

    def llm_for(jid):
        llm.job_id = jid
        return llm

    ctx = {"sessionmaker": sessionmaker, "redis": redis, "gh_for": lambda _: gh, "llm_for": llm_for}
    await run_review(ctx, job_id)


async def load(session, job_id):
    async with session.begin():
        return await repo.get_job(session, uuid.UUID(job_id))


async def test_happy_path_runs_to_done(session, sessionmaker):
    job_id = await seed(session)
    gh = FakeGitHub()
    await run(sessionmaker, gh, FakeRedis(), job_id)
    job = await load(session, job_id)
    assert job.state == JobState.DONE
    assert job.finished_at is not None
    # Nothing to say: no review, but a summary and a green check
    assert gh.created_reviews == []
    assert "No issues found" in gh.issue_comments[0][1]
    assert gh.check_runs == [("create", "Reviewing…"), ("complete", "success", "No issues found")]
    assert (job.check_run_id, job.summary_comment_id) == (777, 4242)


async def test_newer_push_cancels_job(session, sessionmaker):
    job_id = await seed(session)
    await run(sessionmaker, FakeGitHub(), FakeRedis(latest="c" * 40), job_id)
    assert (await load(session, job_id)).state == JobState.CANCELLED


async def test_cancel_after_start_closes_check_run_neutral(session, sessionmaker):
    job_id = await seed(session)
    gh = FakeGitHub(head_sha="c" * 40)  # check run is created, then fetch sees the PR moved
    await run(sessionmaker, gh, FakeRedis(), job_id)
    assert gh.check_runs[-1] == ("complete", "neutral", "Superseded by a newer push")


async def test_moved_pr_head_cancels_job(session, sessionmaker):
    job_id = await seed(session)
    await run(sessionmaker, FakeGitHub(head_sha="c" * 40), FakeRedis(), job_id)
    assert (await load(session, job_id)).state == JobState.CANCELLED


async def test_transient_errors_are_retried(session, sessionmaker, monkeypatch):
    monkeypatch.setattr(orchestrator, "RETRY_DELAYS_S", (0, 0, 0))  # don't actually wait
    job_id = await seed(session)
    gh = FakeGitHub(transient_failures=2)
    await run(sessionmaker, gh, FakeRedis(), job_id)
    assert (await load(session, job_id)).state == JobState.DONE
    assert gh.pull_calls == 3


async def test_endless_transient_errors_fail_the_job(session, sessionmaker, monkeypatch):
    monkeypatch.setattr(orchestrator, "RETRY_DELAYS_S", (0, 0, 0))
    job_id = await seed(session)
    await run(sessionmaker, FakeGitHub(transient_failures=99), FakeRedis(), job_id)
    job = await load(session, job_id)
    assert job.state == JobState.FAILED
    assert "still failing" in job.error


async def test_fatal_error_fails_with_reason(session, sessionmaker):
    job_id = await seed(session)
    gh = FakeGitHub(fatal=True)
    await run(sessionmaker, gh, FakeRedis(), job_id)
    job = await load(session, job_id)
    assert job.state == JobState.FAILED
    assert job.error == "PR not found"
    assert gh.check_runs[-1] == ("complete", "neutral", "Review could not complete")  # never "failure"


async def test_drafted_comments_are_anchored_and_saved(session, sessionmaker):
    from sqlalchemy import func, select

    job_id = await seed(session)
    # line 2 is an added line; line 50 is nowhere near the diff
    llm = FakeLLM([comment(2), comment(50), comment(2, confidence=0.1)])
    await run(sessionmaker, FakeGitHub(), FakeRedis(), job_id, llm=llm)

    async with session.begin():
        cands = await repo.get_candidates(session, uuid.UUID(job_id))
        critic_rows = await session.scalar(select(func.count()).select_from(CriticResult))
        llm_rows = await session.scalar(select(func.count()).select_from(LLMCall))
    statuses = sorted((c.payload["line"], c.final_status) for c in cands)
    assert statuses == [(2, CandidateStatus.ACCEPTED), (50, CandidateStatus.DROPPED)]  # low confidence never saved
    assert critic_rows == 2
    assert llm_rows == 3  # one call per category group
    assert (await load(session, job_id)).state == JobState.DONE


async def test_accepted_comment_is_posted_and_recorded(session, sessionmaker):
    from sqlalchemy import select

    from core.db.models import PostedComment

    job_id = await seed(session)
    gh = FakeGitHub()
    # two comments on line 2: ranking keeps only the higher-scoring one
    llm = FakeLLM([comment(2, confidence=0.9), {**comment(2, confidence=0.6), "claim": "other issue"}])
    await run(sessionmaker, gh, FakeRedis(), job_id, llm=llm)

    [review] = gh.created_reviews
    [inline] = review["comments"]
    assert review["commit_id"] == HEAD
    assert (inline["path"], inline["line"], inline["side"]) == ("app.py", 2, "RIGHT")
    assert "problem on line 2" in inline["body"]
    assert gh.check_runs[-1] == ("complete", "neutral", "1 comment, 0 verified")

    async with session.begin():
        posted = (await session.scalars(select(PostedComment))).all()
    assert [(p.github_comment_id, p.review_id, p.commit_sha) for p in posted] == [(5000, 900, HEAD)]


async def test_existing_bot_review_on_same_commit_is_not_duplicated(session, sessionmaker):
    job_id = await seed(session)
    # a previous attempt crashed after posting this review but before saving it
    earlier = {"id": 321, "commit_id": HEAD, "user": {"login": get_settings().bot_login}}
    gh = FakeGitHub(existing_reviews=[earlier])
    await run(sessionmaker, gh, FakeRedis(), job_id, llm=FakeLLM([comment(2)]))
    assert gh.created_reviews == []
    assert (await load(session, job_id)).state == JobState.DONE


async def test_issue_posted_by_earlier_push_is_not_reposted(session, sessionmaker):
    first_job = await seed(session)
    await run(sessionmaker, FakeGitHub(), FakeRedis(), first_job, llm=FakeLLM([comment(2)]))

    # second push to the same PR: the model reports the same issue again
    async with session.begin():
        job, _ = await repo.create_job(session, 10, 1, "a" * 40, "c" * 40, JobTrigger.SYNCHRONIZE)
    gh = FakeGitHub(head_sha="c" * 40)
    await run(sessionmaker, gh, FakeRedis(latest="c" * 40), str(job.id), llm=FakeLLM([comment(2)]))
    assert gh.created_reviews == []
    assert "No issues found" in gh.issue_comments[0][1]
