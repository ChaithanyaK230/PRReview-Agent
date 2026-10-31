import hashlib
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, replace

from core.agent.schemas import CandidateComment

SEVERITY_WEIGHT = {"high": 2.0, "medium": 1.0, "low": 0.0}
PROXIMITY_LINES = 3  # comments this close on the same file count as duplicates
# Not in LLD: among duplicates at one spot, prefer the one whose fix was tested. Kept below one severity step
# so a proven fix never outranks a more severe issue. Live PR #16 posted the patch-less twin of a verified fix.
FIX_BONUS = 0.5


@dataclass(frozen=True)
class Ranked:
    candidate_id: uuid.UUID
    comment: CandidateComment
    verified: bool
    score: float
    claim_hash: str
    fix_verified: bool = False  # comment.suggested_patch applied and kept tests green: may be one-click


def claim_hash(c: CandidateComment) -> str:
    """Identifies "the same issue" across pushes so it is never re-posted (LLD §14)."""
    normalized = re.sub(r"\s+", " ", c.claim.strip().lower())
    return hashlib.sha1(f"{c.path}|{normalized}".encode()).hexdigest()


def score(c: CandidateComment, verified: bool, fix_verified: bool = False) -> float:
    return 3.0 * verified + SEVERITY_WEIGHT[c.severity] + c.confidence + FIX_BONUS * fix_verified


def select(
    candidates: Iterable[tuple[uuid.UUID, CandidateComment, bool]], posted_hashes: set[str], cap: int,
    fix_verified: frozenset[uuid.UUID] = frozenset(),
) -> list[Ranked]:
    """Pick what to post from accepted candidates given as (id, comment, verified)."""
    pool = []
    for cid, c, verified in candidates:
        h = claim_hash(c)
        if h in posted_hashes:
            continue
        fixed = cid in fix_verified
        pool.append(Ranked(cid, c, verified, score(c, verified, fixed), h, fixed))
    pool.sort(key=lambda r: r.score, reverse=True)

    kept: list[Ranked] = []
    for r in pool:
        i = next((i for i, k in enumerate(kept) if k.comment.path == r.comment.path
                  and abs(k.comment.line - r.comment.line) <= PROXIMITY_LINES), None)
        if i is None:
            kept.append(r)
        else:  # precision over recall: one comment per spot, the best-scoring one
            kept[i] = _adopt_fix(kept[i], r)
    return kept[:cap]


def _adopt_fix(winner: Ranked, twin: Ranked) -> Ranked:
    """A losing duplicate on the same line may hand its tested fix to the winner.

    Live PR #17: the twin WITH a fix also faced bug_proof, which can't prove a syntax error, so it was
    downgraded and lost to a patch-less twin that never faced bug_proof. Its working fix was thrown away.
    """
    if winner.comment.suggested_patch or not twin.fix_verified or twin.comment.line != winner.comment.line:
        return winner
    patched = winner.comment.model_copy(update={"suggested_patch": twin.comment.suggested_patch})
    return replace(winner, comment=patched, fix_verified=True)
