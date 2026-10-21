from dataclasses import dataclass, field

from unidiff import PatchSet

from core.github.client import PullRequest
from core.github.diffmap import DiffMap

# Phase 1 context is diff-only. Symbols, callers and tool findings come later (LLD §11).
MAX_DIFF_CHARS = 120_000  # ~30k tokens
MAX_BODY_CHARS = 2_000


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


def build_context(pr: PullRequest, diff_text: str, diff: DiffMap) -> ReviewContext:
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
    return ReviewContext(text="\n\n".join(parts), skipped_files=skipped)
