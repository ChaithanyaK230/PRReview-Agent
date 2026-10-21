from core.agent.schemas import CandidateComment
from core.critics.base import CriticVerdict
from core.github.diffmap import DiffMap


def check_anchor(c: CandidateComment, diff: DiffMap) -> tuple[CandidateComment, CriticVerdict]:
    """Comment must sit on a line GitHub will accept; nudge it up to 3 lines if close (LLD §13.2)."""
    if c.start_line is not None and (c.start_line >= c.line or not diff.commentable(c.path, c.start_line, c.side)):
        c = c.model_copy(update={"start_line": None})  # fall back to a single-line comment

    if diff.commentable(c.path, c.line, c.side):
        return c, CriticVerdict(critic="anchor", passed=True)

    near = diff.nearest(c.path, c.line, c.side)
    if near is not None:
        moved = c.model_copy(update={"line": near, "start_line": None})
        return moved, CriticVerdict(critic="anchor", passed=True, feedback=f"re-anchored {c.line} -> {near}")

    lines = diff.right if c.side == "RIGHT" else diff.left
    nearby = sorted(lines.get(c.path, frozenset()))[:20]
    return c, CriticVerdict(
        critic="anchor", passed=False,
        feedback=f"Line {c.line} of {c.path} is not in the diff. Commentable lines: {nearby}",
    )
