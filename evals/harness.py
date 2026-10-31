"""Builds eval cases into PR-shaped inputs, and scores what the bot posted (LLD §19.1)."""

import difflib
import hashlib
import io
import itertools
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from core.github.client import PullRequest

DATASET = Path(__file__).parent / "dataset.yaml"
_ids = itertools.count(1_000_000)  # fake GitHub ids for reviews and comments


@dataclass(frozen=True)
class Expected:
    path: str
    lo: int
    hi: int
    categories: tuple[str, ...]


@dataclass(frozen=True)
class Case:
    id: str
    title: str
    body: str
    base: dict[str, str]
    head: dict[str, str]  # full head tree: base with the case's changed files applied
    expected: list[Expected]

    @property
    def base_sha(self) -> str:
        return hashlib.sha1(f"{self.id}:base".encode()).hexdigest()

    @property
    def head_sha(self) -> str:
        return hashlib.sha1(f"{self.id}:head".encode()).hexdigest()


@dataclass(frozen=True)
class Posted:
    path: str
    line: int
    category: str
    verified: bool


@dataclass
class CaseScore:
    true_positives: int = 0
    false_positives: int = 0
    found: int = 0  # expected items matched
    expected: int = 0
    unmatched_posts: list[Posted] = field(default_factory=list)


def load_cases(path: Path = DATASET) -> list[Case]:
    cases = []
    for raw in yaml.safe_load(path.read_text(encoding="utf-8")):
        base = dict(raw["base"])
        cases.append(Case(
            id=raw["id"], title=raw["title"], body=raw.get("body", ""),
            base=base, head={**base, **raw["head"]},  # files a case doesn't list are unchanged
            expected=[Expected(e["path"], e["line_range"][0], e["line_range"][1], tuple(e["category"]))
                      for e in raw["expected"]],
        ))
    return cases


def make_diff(base: dict[str, str], head: dict[str, str]) -> str:
    """A git-style unified diff, the same shape GitHub returns for a PR."""
    out: list[str] = []
    for path in sorted(set(base) | set(head)):
        before, after = base.get(path), head.get(path)
        if before == after:
            continue
        out.append(f"diff --git a/{path} b/{path}\n")
        if before is None:
            out.append("new file mode 100644\n")
        out += difflib.unified_diff(
            (before or "").splitlines(keepends=True), (after or "").splitlines(keepends=True),
            fromfile=f"a/{path}" if before is not None else "/dev/null",
            tofile=f"b/{path}" if after is not None else "/dev/null",
        )
    return "".join(out)


def make_tarball(files: dict[str, str], sha: str) -> bytes:
    """Like GitHub's /tarball endpoint: everything inside one top-level folder."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(f"evals-repo-{sha[:7]}/{path}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def score_case(expected: list[Expected], posted: list[Posted]) -> CaseScore:
    """Greedy matching: each expected item can be found by at most one posted comment."""
    score = CaseScore(expected=len(expected))
    unmatched = list(expected)
    for p in posted:
        hit = next((e for e in unmatched
                    if e.path == p.path and e.lo <= p.line <= e.hi and p.category in e.categories), None)
        if hit is None:
            score.false_positives += 1
            score.unmatched_posts.append(p)
        else:
            unmatched.remove(hit)
            score.true_positives += 1
    score.found = score.expected - len(unmatched)
    return score


class EvalGitHub:
    """Serves one case as if it were a GitHub PR, and records everything the bot posts."""

    def __init__(self, case: Case, bot_login: str) -> None:
        self.case = case
        self.bot_login = bot_login
        self.diff = make_diff(case.base, case.head)
        self.tarballs = {case.base_sha: make_tarball(case.base, case.base_sha),
                         case.head_sha: make_tarball(case.head, case.head_sha)}
        self.reviews: list[dict] = []
        self.review_comments: dict[int, list[dict]] = {}
        self.summary = ""

    async def get_pull(self, repo, number):
        return PullRequest(number=number, title=self.case.title, body=self.case.body, author="eval-author",
                           draft=False, state="open", base_ref="main", base_sha=self.case.base_sha,
                           head_sha=self.case.head_sha)

    async def get_diff(self, repo, number):
        return self.diff

    async def get_file(self, repo, path, ref):
        return None  # no .prreview.yml: defaults

    async def download_tarball(self, repo, ref):
        return self.tarballs[ref]

    async def list_reviews(self, repo, number):
        return self.reviews

    async def create_review(self, repo, number, commit_sha, body, comments):
        # Globally unique ids, like GitHub's: posted_comments.github_comment_id is UNIQUE across all
        # jobs, and the first eval run crashed when every case started counting from the same number
        review_id = next(_ids)
        self.reviews.append({"id": review_id, "commit_id": commit_sha, "user": {"login": self.bot_login}})
        self.review_comments[review_id] = [{"id": next(_ids), "body": c["body"]} for c in comments]
        return review_id

    async def list_review_comments(self, repo, number, review_id):
        return self.review_comments.get(review_id, [])

    async def upsert_issue_comment(self, repo, number, marker, body, bot_login):
        self.summary = body
        return 1

    async def create_check_run(self, repo, head_sha, title):
        return 1

    async def complete_check_run(self, repo, check_run_id, conclusion, title, summary):
        pass
