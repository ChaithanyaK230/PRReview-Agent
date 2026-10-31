import json
from pathlib import Path

from pydantic import BaseModel, Field

from core.agent.drafter import StructuredLLM
from core.agent.schemas import CandidateComment
from core.cost import CostMeter
from core.db.models import LLMPurpose

PROMPT = (Path(__file__).parent / "prompts" / "proof_test.md").read_text(encoding="utf-8")
MAX_FILE_CHARS = 30_000


class ProofTest(BaseModel):
    content: str = Field(max_length=20_000)  # a complete pytest module
    explanation: str = Field(default="", max_length=500)


def import_hint(path: str) -> str:
    """'src/billing/discount.py' → 'import billing.discount' (src layout is common)."""
    module = path.removesuffix(".py").removeprefix("src/").replace("/", ".").removesuffix(".__init__")
    return f"import {module}  # or: from {module} import <name>"


def _untrusted(tag: str, content: str) -> str:
    safe = content.replace("</untrusted", "<\\/untrusted")
    return f"<untrusted_{tag}>\n{safe}\n</untrusted_{tag}>"


class ProofWriter:
    """Asks the LLM for a test that fails on the PR's code and passes with the fix (LLD §13.4)."""

    def __init__(self, llm: StructuredLLM, meter: CostMeter) -> None:
        self.llm = llm
        self.meter = meter
        self.system = PROMPT.replace("{schema}", json.dumps(ProofTest.model_json_schema()))

    async def write(self, comment: CandidateComment, file_text: str, previous_error: str | None = None) -> ProofTest:
        parts = [
            f"Bug report on {comment.path} line {comment.line}: {comment.claim}\n\n{comment.body}",
            f"Import the code under test like this:\n{import_hint(comment.path)}",
            f"Current code of {comment.path}:\n" + _untrusted("source", file_text[:MAX_FILE_CHARS]),
            f"Proposed fix (unified diff):\n{comment.suggested_patch or '(none)'}",
        ]
        if previous_error:
            # The first attempt couldn't even run: show why, once (LLD §13.4: "revise test once")
            parts.append("Your previous test did not work. pytest said:\n" + _untrusted("pytest_output", previous_error))
        return await self.llm.structured(LLMPurpose.PROOF_TEST, self.system, "\n\n".join(parts), ProofTest, self.meter)
