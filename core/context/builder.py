from dataclasses import dataclass, field

from unidiff import PatchSet

from core.analysis.evidence import Evidence
from core.github.client import PullRequest
from core.github.diffmap import DiffMap

# Context = PR text + diff + tool evidence. Symbols and callers come later (LLD §11).
MAX_DIFF_CHARS = 120_000  # ~30k tokens
MAX_BODY_CHARS = 2_000
MAX_TEST_LINES = 60  # per broken test (LLD §11.2)
MAX_TESTS = 10
MAX_FINDINGS = 30

CHANGE_LABEL = {
    "newly_failing": "passed before this PR, fails now",
    "new_test_failing": "test added by this PR, fails",
    "failure_changed": "already failed before, but now fails differently",
}


@dataclass(frozen=True)
class ReviewContext:
    text: str
    skipped_files: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.text


def _untrusted(tag: str, content: str) -> str:
    # Stop PR text from closing our delimiter early and smuggling in "instructions"
    safe = content.replace("</untrusted", "<\\/untrusted")
    return f"<untrusted_{tag}>\n{safe}\n</untrusted_{tag}>"


def render_diff(diff_text: str, diff: DiffMap) -> tuple[str, list[str]]:
    """Diff with new-file line numbers, so the LLM can anchor comments. Returns (text, skipped paths)."""
    blocks: list[str] = []
    skipped: list[str] = []
    used = 0
    for pf in PatchSet.from_string(diff_text):
        path = pf.source_file.removeprefix("a/") if pf.is_removed_file else pf.target_file.removeprefix("b/")
        if path not in diff.right:
            continue  # binary or oversized: DiffMap already excluded it
        lines = [f"### {path}"]
        for hunk in pf:
            lines.append("@@")
            for line in hunk:
                text = line.value.rstrip("\r\n")
                if line.is_added:
                    lines.append(f"{line.target_line_no:>5} + {text}")
                elif line.is_removed:
                    lines.append(f"      - {text}")
                elif line.is_context:
                    lines.append(f"{line.target_line_no:>5}   {text}")
        block = "\n".join(lines)
        if used + len(block) > MAX_DIFF_CHARS:
            skipped.append(path)
            continue
        blocks.append(block)
        used += len(block)
    return "\n\n".join(blocks), skipped


def render_evidence(evidence: Evidence) -> str:
    """Tool results in LLD §11.2 priority order: broken tests first, then new findings."""
    if not evidence.ran:
        return f"No tool evidence: tests and linters were not run ({evidence.note or 'no sandbox'})."
    d = evidence.delta
    body: list[str] = []
    if d.test_changes:
        body.append("## Tests broken by this PR")
        for change in d.test_changes[:MAX_TESTS]:
            body.append(f"- {change.test.test_id} ({CHANGE_LABEL[change.kind]})")
            body += ["    " + ln for ln in (change.test.message or "").splitlines()[-MAX_TEST_LINES:]]
    if d.new_findings:
        body.append("## New lint findings on lines this PR added")
        body += [f"- {f.path}:{f.line} {f.tool} {f.rule}: {f.message}" for f in d.new_findings[:MAX_FINDINGS]]
    if not body:
        body.append("No new test failures or lint findings.")
    if d.unavailable:
        body.append(f"Not compared (no reliable result on one side): {', '.join(sorted(set(d.unavailable)))}")
    header = ("Results of running this repository's own tests and linters on the base and head commits. "
              "Pre-existing problems were removed: everything listed was introduced by this PR.")
    # Test output can contain text the PR author controls, so it is untrusted like the PR body
    return f"{header}\n{_untrusted('tool_output', chr(10).join(body))}"


def build_context(
    pr: PullRequest, diff_text: str, diff: DiffMap, evidence: Evidence | None = None
) -> ReviewContext:
    rendered, skipped = render_diff(diff_text, diff)
    if not rendered:
        return ReviewContext(text="", skipped_files=skipped)
    parts = [
        _untrusted("pr_title", pr.title),
        _untrusted("pr_body", pr.body[:MAX_BODY_CHARS]),
        "Diff. The number on the left is the new-file line number; '+' lines were added by this PR.",
        _untrusted("diff", rendered),
    ]
    if skipped:
        parts.append(f"(Not shown, too large: {', '.join(skipped)})")
    if evidence is not None:
        parts.append(render_evidence(evidence))
    return ReviewContext(text="\n\n".join(parts), skipped_files=skipped)
