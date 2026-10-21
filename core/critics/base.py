from pydantic import BaseModel


class CriticVerdict(BaseModel):
    """Result of one critic on one candidate (LLD §13.1; named to avoid clashing with the critic_results table)."""

    critic: str
    passed: bool
    feedback: str = ""
    verified: bool = False
    duration_ms: int = 0
