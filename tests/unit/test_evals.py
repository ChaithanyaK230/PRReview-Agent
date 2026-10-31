"""The eval harness itself: dataset sanity, diff building, and scoring."""

import io
import tarfile

import pytest

from core.github.diffmap import DiffMap
from evals.harness import Expected, Posted, load_cases, make_diff, make_tarball, score_case

CASES = load_cases()


def test_dataset_loads_with_unique_ids():
    assert len(CASES) >= 7
    assert len({c.id for c in CASES}) == len(CASES)
    assert any(not c.expected for c in CASES)  # at least one clean case to catch false positives


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_expected_ranges_point_at_commentable_lines(case):
    """Guards the dataset: a typo'd line range would silently skew precision and recall.

    "Commentable", not "added": a PR that only DELETES code (e.g. removes a None check) has no
    added lines, but GitHub accepts comments on the surrounding context lines.
    """
    diff = DiffMap.from_diff(make_diff(case.base, case.head))
    assert diff.right, "the case's head must change something"
    for e in case.expected:
        lines = diff.right.get(e.path, frozenset())
        assert any(e.lo <= line <= e.hi for line in lines), f"{e} covers no commentable line ({sorted(lines)})"


def test_unchanged_files_carry_over_to_head():
    case = next(c for c in CASES if c.id == "off-by-one-pagination")
    assert case.head["test_pagination.py"] == case.base["test_pagination.py"]


def test_tarball_has_one_top_level_folder():
    with tarfile.open(fileobj=io.BytesIO(make_tarball({"a.py": "x = 1\n"}, "abcdef0")), mode="r:gz") as tar:
        assert tar.getnames() == ["evals-repo-abcdef0/a.py"]


E = Expected("app.py", 10, 12, ("bug",))


def test_score_true_and_false_positives():
    score = score_case([E], [Posted("app.py", 11, "bug", False), Posted("app.py", 40, "bug", False)])
    assert (score.true_positives, score.false_positives, score.found) == (1, 1, 1)


def test_score_wrong_category_or_file_is_a_miss():
    score = score_case([E], [Posted("app.py", 11, "perf", False), Posted("other.py", 11, "bug", False)])
    assert (score.true_positives, score.false_positives, score.found) == (0, 2, 0)


def test_one_expected_item_cannot_be_matched_twice():
    score = score_case([E], [Posted("app.py", 10, "bug", False), Posted("app.py", 12, "bug", False)])
    assert (score.true_positives, score.false_positives) == (1, 1)


def test_any_comment_on_a_clean_case_is_a_false_positive():
    score = score_case([], [Posted("app.py", 1, "bug", False)])
    assert (score.false_positives, score.expected) == (1, 0)


async def test_fake_github_ids_are_unique_across_cases():
    """The first full eval run crashed: two cases produced the same comment id (a UNIQUE column)."""
    from evals.harness import EvalGitHub

    a, b = EvalGitHub(CASES[0], "bot"), EvalGitHub(CASES[1], "bot")
    ids = []
    for gh in (a, b):
        review = await gh.create_review("r", 1, "sha", "", [{"body": "x"}, {"body": "y"}])
        ids += [review] + [c["id"] for c in await gh.list_review_comments("r", 1, review)]
    assert len(ids) == len(set(ids))
