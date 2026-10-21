import hashlib
import hmac
import json

import httpx
import pytest

from api.deps import get_queue, get_session
from api.main import create_app
from core.settings import get_settings
from tests.factories import pr_payload


def signed_headers(body: bytes, delivery: str = "delivery-1", event: str = "ping") -> dict[str, str]:
    secret = get_settings().github_webhook_secret.get_secret_value().encode()
    return {
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery,
        "X-Hub-Signature-256": "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest(),
        "Content-Type": "application/json",
    }


class FakeQueue:
    def __init__(self):
        self.kv: dict[str, str] = {}
        self.jobs: list[tuple] = []

    async def set(self, key, value):
        self.kv[key] = value

    async def enqueue_job(self, fn, *args, _job_id=None):
        self.jobs.append((fn, args, _job_id))


@pytest.fixture
def queue():
    return FakeQueue()


@pytest.fixture
async def client(session, queue):
    app = create_app()

    async def _test_session():
        yield session

    app.dependency_overrides[get_session] = _test_session  # use the test DB
    app.dependency_overrides[get_queue] = lambda: queue    # no real Redis
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def post_pr(client, payload, delivery):
    body = json.dumps(payload).encode()
    return await client.post(
        "/webhooks/github", content=body, headers=signed_headers(body, delivery=delivery, event="pull_request")
    )


async def test_valid_delivery_accepted_then_deduped(client):
    body = json.dumps({"zen": "Keep it simple."}).encode()
    first = await client.post("/webhooks/github", content=body, headers=signed_headers(body))
    again = await client.post("/webhooks/github", content=body, headers=signed_headers(body))
    assert first.status_code == 202
    assert again.status_code == 200
    assert again.json() == {"status": "duplicate"}


async def test_bad_signature_rejected(client):
    body = b"{}"
    headers = signed_headers(body)
    headers["X-Hub-Signature-256"] = "sha256=" + "0" * 64
    resp = await client.post("/webhooks/github", content=body, headers=headers)
    assert resp.status_code == 401


async def test_pr_opened_creates_and_enqueues_job(client, queue):
    resp = await post_pr(client, pr_payload(), "d-pr-1")
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]
    assert queue.kv["pr:555:1:latest"] == "b" * 40
    assert queue.jobs == [("run_review", (job_id,), f"555:1:{'b' * 40}")]


async def test_same_commit_twice_enqueues_once(client, queue):
    await post_pr(client, pr_payload(), "d-pr-1")
    await post_pr(client, pr_payload(action="reopened"), "d-pr-2")  # new delivery, same commit
    assert len(queue.jobs) == 1


async def test_draft_pr_is_ignored(client, queue):
    resp = await post_pr(client, pr_payload(draft=True), "d-pr-3")
    assert resp.json() == {"status": "ignored"}
    assert queue.jobs == []