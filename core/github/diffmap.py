from dataclasses import dataclass
from typing import Literal

from unidiff import PatchSet

Side = Literal["LEFT", "RIGHT"]

# Very large files (generated code, lockfiles) are skipped (LLD §8.3)
MAX_CHANGED_LINES_PER_FILE = 5000


@dataclass(frozen=True)
class DiffMap:
    right: dict[str, frozenset[int]]  # path -> new-side lines (added + context)
    left: dict[str, frozenset[int]]  # path -> old-side lines (removed + context)
    added: dict[str, frozenset[int]]  # path -> added lines only
    renamed: dict[str, str]  # new path -> old path

    @classmethod
    def from_diff(cls, diff_text: str) -> "DiffMap":
        right: dict[str, frozenset[int]] = {}
        left: dict[str, frozenset[int]] = {}
        added: dict[str, frozenset[int]] = {}
        renamed: dict[str, str] = {}

        for pf in PatchSet.from_string(diff_text):
            if pf.is_binary_file or pf.added + pf.removed > MAX_CHANGED_LINES_PER_FILE:
                continue

            # unidiff's pf.path returns the OLD path for renames, so derive paths ourselves
            old = pf.source_file.removeprefix("a/")
            new = pf.target_file.removeprefix("b/")
            path = old if pf.is_removed_file else new
            if not pf.is_added_file and not pf.is_removed_file and old != new:
                renamed[new] = old

            r: set[int] = set()
            lf: set[int] = set()
            a: set[int] = set()
            for hunk in pf:
                for line in hunk:
                    if line.is_added:
                        r.add(line.target_line_no)
                        a.add(line.target_line_no)
                    elif line.is_removed:
                        lf.add(line.source_line_no)
                    elif line.is_context:
                        r.add(line.target_line_no)
                        lf.add(line.source_line_no)

            right[path] = frozenset(r)
            left[path] = frozenset(lf)
            added[path] = frozenset(a)

        return cls(right=right, left=left, added=added, renamed=renamed)

    def commentable(self, path: str, line: int, side: Side = "RIGHT") -> bool:
        lines = self.right if side == "RIGHT" else self.left
        return line in lines.get(path, frozenset())

    def nearest(self, path: str, line: int, side: Side = "RIGHT", radius: int = 3) -> int | None:
        """Closest commentable line within `radius`, preferring the line above on ties."""
        for d in range(radius + 1):
            for candidate in (line - d, line + d):
                if self.commentable(path, candidate, side):
                    return candidate
        return None