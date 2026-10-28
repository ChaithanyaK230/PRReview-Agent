from dataclasses import dataclass, field

from core.analysis.delta import Delta


@dataclass(frozen=True)
class Evidence:
    """What running the repo's own tools told us about this PR. Feeds the AI context and the summary."""

    ran: bool  # False = sandbox/tools didn't run at all → diff-only review
    delta: Delta = field(default_factory=Delta)
    note: str = ""  # why the tools didn't run (shown to users and the AI)

    def summary_line(self) -> str:
        if not self.ran:
            return f"Diff-only review: tests and linters were not run ({self.note or 'no sandbox'})."
        d = self.delta
        broken, new = len(d.test_changes), len(d.new_findings)
        parts = [f"{broken} test{'s' if broken != 1 else ''} broken by this PR",
                 f"{new} new lint finding{'s' if new != 1 else ''}"]
        if d.fixed_tests:
            parts.append(f"{len(d.fixed_tests)} fixed")
        line = "Tests and linters ran on base and head: " + ", ".join(parts) + "."
        if d.unavailable:
            line += f" Not compared (no reliable result on one side): {', '.join(sorted(set(d.unavailable)))}."
        return line
