from typing import Literal

from pydantic import BaseModel, Field

Category = Literal["bug", "security", "perf", "test_gap", "style"]


class CandidateComment(BaseModel):
    """One review comment proposed by the LLM (LLD §12.1). Code, not the model, decides if it's posted."""

    path: str
    line: int = Field(ge=1)
    side: Literal["LEFT", "RIGHT"] = "RIGHT"
    start_line: int | None = None
    category: Category
    severity: Literal["high", "medium", "low"]
    claim: str = Field(max_length=200)
    body: str = Field(max_length=2000)
    suggested_patch: str | None = None  # unified diff against head
    evidence_needed: Literal["repro_test", "lint", "none"] = "none"
    confidence: float = Field(ge=0.0, le=1.0)


class DraftResponse(BaseModel):
    comments: list[CandidateComment] = Field(default_factory=list, max_length=15)
