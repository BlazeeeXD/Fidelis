"""Tiny unified-diff reader.

The mock reviewer uses it to anchor its canned findings on lines that really
exist in a PR's diff, so inline-comment posting (Week 6) can be exercised with
real pull requests. The real reviewer can reuse it to tell the LLM which lines
are commentable.
"""

import re
from dataclasses import dataclass

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


@dataclass(frozen=True)
class AddedLine:
    """A line added by the PR: ``file`` is repo-relative, ``line`` is 1-based."""

    file: str
    line: int
    text: str


def parse_added_lines(diff: str) -> list[AddedLine]:
    """Return every added line in ``diff`` with its line number in the new file.

    Handles multiple files and hunks, deleted files (skipped), renames (the new
    path is used) and ``\\ No newline at end of file`` markers. Anything that is
    not a recognisable unified diff yields an empty list rather than an error.
    """
    added: list[AddedLine] = []
    current_file: str | None = None
    new_line = 0
    in_hunk = False

    for raw in diff.splitlines():
        if raw.startswith("diff --git "):
            current_file, in_hunk = None, False
            continue
        if not in_hunk and raw.startswith("+++ "):
            target = raw[4:].split("\t")[0].strip()
            current_file = None if target == "/dev/null" else target.removeprefix("b/")
            continue
        if raw.startswith("--- ") and not in_hunk:
            continue

        hunk = _HUNK_RE.match(raw)
        if hunk:
            new_line = int(hunk.group(1))
            in_hunk = True
            continue
        if not in_hunk or current_file is None:
            continue

        if raw.startswith("+"):
            added.append(AddedLine(current_file, new_line, raw[1:]))
            new_line += 1
        elif raw.startswith("-"):
            continue  # removed line: does not exist in the new file
        elif raw.startswith("\\"):
            continue  # "\ No newline at end of file"
        else:
            new_line += 1  # context line

    return added
