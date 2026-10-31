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


def test_twin_with_a_tested_fix_wins_but_never_beats_higher_severity():
    """Live PR #16: two drafts of the same bug, only one carrying the verified `a * b` fix."""
    (bare_id, bare, _), (fix_id, with_fix, _) = items(cc(6, "high", 0.95), cc(6, "high", 0.9))
    ranked = select([(bare_id, bare, False), (fix_id, with_fix, False)], set(), 7, fix_verified=frozenset({fix_id}))
    assert [r.candidate_id for r in ranked] == [fix_id]
    (high_id, high, _), (med_id, med, _) = items(cc(1, "high", 0.5), cc(20, "medium", 0.9))
    ranked = select([(high_id, high, False), (med_id, med, False)], set(), 7, fix_verified=frozenset({med_id}))
    assert ranked[0].candidate_id == high_id  # 2.5 vs 1.9 + 0.5


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


def test_winner_adopts_the_tested_fix_of_a_duplicate_on_the_same_line():
    """Live PR #17: the patch-less twin won; the downgraded twin's verified fix was thrown away."""
    winner_id, loser_id = uuid.uuid4(), uuid.uuid4()
    bare = cc(6, "high", 0.95, claim="undefined name c")
    fixed = cc(6, "medium", 0.9, claim="c is undefined").model_copy(update={"suggested_patch": "@@ -6 +6 @@\n-x\n+y\n"})
    ranked = select([(winner_id, bare, False), (loser_id, fixed, False)], set(), 7,
                    fix_verified=frozenset({loser_id}))
    assert len(ranked) == 1 and ranked[0].candidate_id == winner_id
    assert ranked[0].comment.claim == "undefined name c"            # the winner's words and severity stay
    assert ranked[0].comment.suggested_patch == fixed.suggested_patch and ranked[0].fix_verified


def test_no_adoption_from_another_line_or_an_unverified_fix():
    winner_id, other_id = uuid.uuid4(), uuid.uuid4()
    patched = cc(7, "low", 0.1).model_copy(update={"suggested_patch": "@@ -7 +7 @@\n-x\n+y\n"})
    ranked = select([(winner_id, cc(6, "high", 0.9), False), (other_id, patched, False)], set(), 7,
                    fix_verified=frozenset({other_id}))
    assert ranked[0].comment.suggested_patch is None and not ranked[0].fix_verified  # line 7 != line 6
    same_line = cc(6, "low", 0.1).model_copy(update={"suggested_patch": "@@ -6 +6 @@\n-x\n+y\n"})
    ranked = select([(winner_id, cc(6, "high", 0.9), False), (other_id, same_line, False)], set(), 7)
    assert ranked[0].comment.suggested_patch is None  # its fix was never verified
