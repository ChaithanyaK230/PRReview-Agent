"""Turn an LLM-written patch into one GNU `patch` can apply.

Verified on 2026-10-28 in the E2B sandbox: LLM patches often lack ---/+++ headers, miscount
the @@ line numbers, or have context only above the change. `git apply` rejects all three;
GNU `patch` accepts the last one (with fuzz 1) once headers and counts are fixed here.
"""

import re

HUNK_HEADER = re.compile(r"^@@ -(?P<old>\d+)(?:,\d+)? \+(?P<new>\d+)(?:,\d+)? @@")
CODE_FENCE = re.compile(r"^```[\w-]*$")
FILE_HEADER_PREFIXES = ("diff --git", "index ", "--- ", "+++ ", "new file mode", "deleted file mode")


class PatchError(ValueError):
    """The patch can't be made applicable; the message is written for the reviser LLM."""


def normalize_patch(patch: str, path: str) -> str:
    lines = [ln for ln in patch.replace("\r\n", "\n").split("\n") if not CODE_FENCE.match(ln.strip())]

    targets = {ln[4:].strip().removeprefix("b/") for ln in lines if ln.startswith("+++ ")}
    targets.discard("/dev/null")
    if targets - {path}:
        # One comment = one file: a fix spread over files can't be a GitHub suggestion and is risky
        raise PatchError(f"the patch must only change {path}, but it changes {sorted(targets)}")

    hunks: list[tuple[int, int, list[str]]] = []
    for ln in lines:
        header = HUNK_HEADER.match(ln)
        if header:
            hunks.append((int(header["old"]), int(header["new"]), []))
        elif not hunks or ln.startswith(FILE_HEADER_PREFIXES):
            continue  # file headers or text before the first hunk: we write our own headers
        elif ln == "":
            hunks[-1][2].append(" ")  # a blank context line whose leading space got lost
        elif ln[0] in " +-\\":
            hunks[-1][2].append(ln)
        else:
            raise PatchError(f"line {ln[:60]!r} is not part of a unified diff hunk")

    if not hunks:
        raise PatchError("no @@ hunk found; suggested_patch must be a unified diff")

    out = [f"--- a/{path}", f"+++ b/{path}"]
    for old_start, new_start, body in hunks:
        while body and body[-1] == " ":
            body.pop()  # trailing blank context (often just the final newline): dropping it only reduces context
        if not any(ln[0] in "+-" for ln in body):
            raise PatchError("a hunk has no added or removed lines")
        old_count = sum(ln[0] in " -" for ln in body)
        new_count = sum(ln[0] in " +" for ln in body)
        out.append(f"@@ -{old_start},{old_count} +{new_start},{new_count} @@")  # recounted, LLMs miscount
        out += body
    return "\n".join(out) + "\n"
