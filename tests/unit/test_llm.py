import json
import uuid
from decimal import Decimal

import httpx2  # the openai SDK (v3+) uses httpx2, so respx can't intercept it; we inject a mock transport
import pytest
from openai import AsyncOpenAI

from core.agent.llm import LLM
from core.agent.schemas import DraftResponse
from core.cost import CostMeter
from core.db.models import LLMPurpose
from core.errors import BudgetExceeded, FatalError, LLMOutputError, TransientError

VALID = {"comments": [{"path": "a.py", "line": 3, "category": "bug", "severity": "high",
                       "claim": "c", "body": "b", "confidence": 0.8}]}


def completion(content: str, cost: float | None = 0.0002) -> httpx2.Response:
    usage = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
    if cost is not None:
        usage["cost"] = cost
    return httpx2.Response(200, json={
        "id": "x", "object": "chat.completion", "created": 0, "model": "m",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": content}}],
        "usage": usage,
    })


class FakeOpenRouter:
    """Replays scripted responses in order and keeps every request it received."""

    def __init__(self, *responses: httpx2.Response) -> None:
        self.responses = list(responses)
        self.requests: list[dict] = []

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(json.loads(request.content))
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


async def make_llm(server: FakeOpenRouter) -> LLM:
    http = httpx2.AsyncClient(transport=httpx2.MockTransport(server.handle))
    client = AsyncOpenAI(base_url="https://openrouter.test/api/v1", api_key="test", max_retries=0, http_client=http)
    return LLM(client, "deepseek/test", uuid.uuid4())


async def ask(llm, meter=None):
    return await llm.structured(LLMPurpose.DRAFT, "sys", "user", DraftResponse, meter or CostMeter(Decimal("1")))


async def test_valid_json_is_parsed_and_cost_recorded():
    llm = await make_llm(FakeOpenRouter(completion(json.dumps(VALID))))
    meter = CostMeter(Decimal("1"))
    result = await ask(llm, meter)
    assert result.comments[0].line == 3
    assert meter.spent == Decimal("0.0002")
    [record] = llm.drain_records()
    assert (record.input_tokens, record.output_tokens, record.cost_usd) == (100, 20, Decimal("0.0002"))


async def test_request_asks_for_json_and_cost_reporting():
    server = FakeOpenRouter(completion(json.dumps(VALID)))
    await ask(await make_llm(server))
    sent = server.requests[0]
    assert sent["response_format"] == {"type": "json_object"}
    assert sent["usage"] == {"include": True}
    assert sent["model"] == "deepseek/test"


async def test_code_fences_are_stripped():
    llm = await make_llm(FakeOpenRouter(completion("```json\n" + json.dumps(VALID) + "\n```")))
    assert len((await ask(llm)).comments) == 1


async def test_invalid_output_is_retried_once_with_the_error():
    server = FakeOpenRouter(completion("not json"), completion(json.dumps(VALID)))
    assert len((await ask(await make_llm(server))).comments) == 1
    assert len(server.requests) == 2
    assert "invalid" in server.requests[1]["messages"][-1]["content"]


async def test_invalid_output_twice_raises():
    llm = await make_llm(FakeOpenRouter(completion('{"comments": "nope"}')))
    with pytest.raises(LLMOutputError):
        await ask(llm)
    assert len(llm.drain_records()) == 2  # both calls still cost money and are recorded


async def test_missing_cost_uses_pessimistic_estimate():
    llm = await make_llm(FakeOpenRouter(completion(json.dumps(VALID), cost=None)))
    meter = CostMeter(Decimal("1"))
    await ask(llm, meter)
    assert meter.spent == Decimal("0.0006")  # 100 * $3/M + 20 * $15/M


async def test_rate_limit_is_transient():
    llm = await make_llm(FakeOpenRouter(httpx2.Response(429, json={"error": {"message": "slow down"}})))
    with pytest.raises(TransientError):
        await ask(llm)


async def test_out_of_credits_is_fatal():
    llm = await make_llm(FakeOpenRouter(httpx2.Response(402, json={"error": {"message": "insufficient credits"}})))
    with pytest.raises(FatalError):
        await ask(llm)


async def test_exhausted_budget_blocks_the_call():
    server = FakeOpenRouter(completion(json.dumps(VALID)))
    meter = CostMeter(Decimal("0.50"))
    meter.add(Decimal("0.50"))
    with pytest.raises(BudgetExceeded):
        await ask(await make_llm(server), meter)
    assert server.requests == []  # no request was ever sent
