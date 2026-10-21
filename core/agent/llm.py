import json
import re
import uuid
from decimal import Decimal
from typing import Any, TypeVar

import openai
import structlog
from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from core.cost import CostMeter
from core.db.models import LLMCall, LLMPurpose
from core.errors import FatalError, LLMOutputError, TransientError

log = structlog.get_logger()

T = TypeVar("T", bound=BaseModel)

# Only used if OpenRouter doesn't report a call's cost. Deliberately pricey (USD per 1M tokens,
# input/output) so the budget is over-counted rather than silently ignored.
FALLBACK_USD_PER_MTOK = (Decimal("3"), Decimal("15"))

_CODE_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


class LLM:
    """OpenRouter chat client that returns schema-validated objects and meters every call (LLD §12.2)."""

    def __init__(self, client: AsyncOpenAI, model: str, job_id: uuid.UUID) -> None:
        self.client = client
        self.model = model
        self.job_id = job_id
        self.records: list[LLMCall] = []

    def drain_records(self) -> list[LLMCall]:
        """llm_calls rows produced so far, to be saved with the step's state transition."""
        out, self.records = self.records, []
        return out

    async def structured(
        self, purpose: LLMPurpose, system: str, user: str, schema: type[T], meter: CostMeter,
        max_tokens: int = 4000,
    ) -> T:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        error = ""
        for _ in range(2):  # one retry with the validation error fed back
            text = await self._complete(purpose, messages, meter, max_tokens)
            try:
                return schema.model_validate(json.loads(_CODE_FENCE.sub("", text.strip())))
            except (json.JSONDecodeError, ValidationError) as e:
                error = str(e)[:1000]
                messages += [
                    {"role": "assistant", "content": text},
                    {"role": "user", "content": f"That reply was invalid: {error}\n"
                                                "Reply again with ONLY the corrected JSON object."},
                ]
        raise LLMOutputError(f"{purpose}: invalid output twice: {error}")

    async def _complete(
        self, purpose: LLMPurpose, messages: list[dict[str, Any]], meter: CostMeter, max_tokens: int
    ) -> str:
        meter.check()
        try:
            resp = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,  # type: ignore[arg-type]
                max_tokens=max_tokens,
                temperature=0.2,
                response_format={"type": "json_object"},
                extra_body={"usage": {"include": True}},  # ask OpenRouter to report the real cost
            )
        except (openai.APIConnectionError, openai.RateLimitError, openai.InternalServerError) as e:
            raise TransientError(f"LLM call failed: {e}") from e
        except openai.APIStatusError as e:
            # 400 bad request, 401 bad key, 402 out of credits: retrying won't help
            raise FatalError(
                f"LLM request rejected ({e.status_code}): {e.message}",
                user_message="The AI service rejected the request.",
            ) from e

        usage = resp.usage
        tokens_in = usage.prompt_tokens if usage else 0
        tokens_out = usage.completion_tokens if usage else 0
        reported = getattr(usage, "cost", None)
        if reported is not None:
            cost = Decimal(str(reported))
        else:
            cost = (tokens_in * FALLBACK_USD_PER_MTOK[0] + tokens_out * FALLBACK_USD_PER_MTOK[1]) / Decimal(1_000_000)
        meter.add(cost)
        self.records.append(LLMCall(
            job_id=self.job_id, purpose=purpose, model=self.model,
            input_tokens=tokens_in, output_tokens=tokens_out, cost_usd=cost,
        ))
        log.info("llm.call", purpose=str(purpose), tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=str(cost))
        return resp.choices[0].message.content or ""
