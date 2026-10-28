import json
from pathlib import Path

from core.agent.drafter import StructuredLLM
from core.agent.schemas import CandidateComment, ReviseResponse
from core.cost import CostMeter
from core.db.models import LLMPurpose
from core.errors import LLMOutputError

PROMPT = (Path(__file__).parent / "prompts" / "revise.md").read_text(encoding="utf-8")


def _untrusted(tag: str, content: str) -> str:
    safe = content.replace("</untrusted", "<\\/untrusted")
    return f"<untrusted_{tag}>\n{safe}\n</untrusted_{tag}>"


class Reviser:
    """Asks the LLM to repair a comment that failed critics (LLD §12.4)."""

    def __init__(self, llm: StructuredLLM, meter: CostMeter, review_context: str) -> None:
        self.llm = llm
        self.meter = meter
        self.review_context = review_context
        self.system = PROMPT.replace("{schema}", json.dumps(ReviseResponse.model_json_schema()))

    async def revise(self, comment: CandidateComment, failures: list) -> ReviseResponse:
        failed = "\n".join(f"- {v.critic}: {v.feedback}" for v in failures)
        user = "\n\n".join([
            self.review_context,
            f"Your comment:\n```json\n{comment.model_dump_json(indent=2)}\n```",
            # Critic feedback quotes test output and patch errors, which the PR author can influence
            "Checks that failed:\n" + _untrusted("check_feedback", failed),
        ])
        try:
            response = await self.llm.structured(LLMPurpose.REVISE, self.system, user, ReviseResponse, self.meter)
        except LLMOutputError as e:
            return ReviseResponse(action="drop", reason=f"reviser output invalid: {str(e)[:200]}")
        if response.action == "revise" and response.comment is None:
            return ReviseResponse(action="drop", reason="reviser said revise but gave no comment")
        return response
