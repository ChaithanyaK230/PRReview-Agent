import uuid

from core.agent.schemas import CandidateComment
from core.ranking import claim_hash, select


def cc(line, severity="medium", confidence=0.5, claim=None, path="a.py", category="bug"):
    return CandidateComment(path=path, line=line, category=category, severity=severity,
                            claim=claim or f"issue at {path}:{line}", body="b", confidence=confidence)


def items(*comments, verified=False):
    return [(uuid.uuid4(), c, verified) for c in comments]


def test_highest_score_first():
    ranked = select(items(cc(1, "low", 0.9), cc(20, "high", 0.5), cc(40, "medium", 0.9)), set(), cap=7)
    assert [r.comment.line for r in ranked] == [20, 40, 1]  # 2.5, 1.9, 0.9


def test_verified_beats_unverified():
    plain = items(cc(1, "high", 1.0))
    verified = items(cc(30, "low", 0.4), verified=True)
    assert select(plain + verified, set(), cap=7)[0].comment.line == 30  # 3.4 vs 3.0


def test_nearby_comments_keep_only_the_best():
    ranked = select(items(cc(4, "high", 1.0), cc(4, "medium", 0.8, category="test_gap"), cc(7, "low")), set(), 7)
    assert [r.comment.line for r in ranked] == [4]


def test_same_line_in_different_files_both_kept():
    assert len(select(items(cc(4, path="a.py"), cc(4, path="b.py")), set(), cap=7)) == 2


def test_already_posted_issue_is_skipped():
    c = cc(4)
    assert select(items(c), {claim_hash(c)}, cap=7) == []


def test_cap():
    assert len(select(items(*(cc(i * 10) for i in range(1, 11))), set(), cap=7)) == 7


def test_claim_hash_ignores_case_and_spacing():
    assert claim_hash(cc(1, claim="Missing  await on send")) == claim_hash(cc(9, claim="missing await on SEND"))
