from core.db.models import JobTrigger
from core.github.events import parse_review_request
from tests.factories import pr_payload

BOT = "prreview-dev-chaithanya[bot]"


def test_opened_pr_starts_review():
    r = parse_review_request("pull_request", pr_payload(), BOT)
    assert r is not None
    assert (r.repo_id, r.pr_number, r.trigger) == (555, 1, JobTrigger.OPENED)


def test_new_push_starts_review():
    assert parse_review_request("pull_request", pr_payload(action="synchronize"), BOT) is not None


def test_skipped_cases():
    assert parse_review_request("pull_request", pr_payload(draft=True), BOT) is None
    assert parse_review_request("pull_request", pr_payload(author=BOT), BOT) is None
    assert parse_review_request("pull_request", pr_payload(author_type="Bot"), BOT) is None
    assert parse_review_request("pull_request", pr_payload(action="closed"), BOT) is None
    assert parse_review_request("check_suite", pr_payload(), BOT) is None