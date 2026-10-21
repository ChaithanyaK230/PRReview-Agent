import hashlib
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass

from core.agent.schemas import CandidateComment

SEVERITY_WEIGHT = {"high": 2.0, "medium": 1.0, "low": 0.0}
PROXIMITY_LINES = 3  # comments this close on the same file count as duplicates


@dataclass(frozen=True)
class Ranked:
    candidate_id: uuid.UUID
    comment: CandidateComment
    verified: bool
    score: float
    claim_hash: str


def claim_hash(c: CandidateComment) -> str:
    """Identifies "the same issue" across pushes so it is never re-posted (LLD §14)."""
    normalized = re.sub(r"\s+", " ", c.claim.strip().lower())
    return hashlib.sha1(f"{c.path}|{normalized}".encode()).hexdigest()


def score(c: CandidateComment, verified: bool) -> float:
    return 3.0 * verified + SEVERITY_WEIGHT[c.severity] + c.confidence


def select(
    candidates: Iterable[tuple[uuid.UUID, CandidateComment, bool]], posted_hashes: set[str], cap: int
) -> list[Ranked]:
    """Pick what to post from accepted candidates given as (id, comment, verified)."""
    pool = []
    for cid, c, verified in candidates:
        h = claim_hash(c)
        if h in posted_hashes:
            continue
        pool.append(Ranked(cid, c, verified, score(c, verified), h))
    pool.sort(key=lambda r: r.score, reverse=True)

    kept: list[Ranked] = []
    for r in pool:
        if any(k.comment.path == r.comment.path and abs(k.comment.line - r.comment.line) <= PROXIMITY_LINES
               for k in kept):
            continue  # precision over recall: one comment per spot, the best-scoring one
        kept.append(r)
    return kept[:cap]
