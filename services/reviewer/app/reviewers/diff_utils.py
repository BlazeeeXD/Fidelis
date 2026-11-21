"""Tiny unified-diff reader.

The mock reviewer uses it to anchor its canned findings on lines that really
exist in a PR's diff, so inline-comment posting can be exercised with
real pull requests. The real reviewer can reuse it to tell the LLM which lines
are commentable.
"""

import ast
import re
from dataclasses import dataclass

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_HUNK_RANGE_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


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
            target = _decode_diff_path(raw[4:].split("\t")[0].strip())
            current_file = None if target == "/dev/null" else target.removeprefix("b/").removeprefix("./")
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


def parse_changed_files(diff: str) -> list[str]:
    """Return unique repo-relative paths present on the new side of a diff.

    Deleted files are omitted because there is no file content at the target
    commit to retrieve. Renames use the new path. Git's quoted path syntax is
    decoded so paths containing spaces or non-ASCII characters remain usable.
    Unsafe paths are ignored rather than passed into a GitHub API URL.
    """
    paths: list[str] = []
    seen: set[str] = set()
    in_hunk = False
    for raw in diff.splitlines():
        if raw.startswith("diff --git "):
            in_hunk = False
            continue
        if raw.startswith("@@ "):
            in_hunk = True
            continue
        if in_hunk or not raw.startswith("+++ "):
            continue
        target = _decode_diff_path(raw[4:].split("\t", 1)[0].strip())
        if target == "/dev/null":
            continue
        path = target.removeprefix("b/").removeprefix("./")
        parts = path.split("/")
        if (
            not path
            or path.startswith("/")
            or "\\" in path
            or any(part in {"", ".", ".."} for part in parts)
        ):
            continue
        if path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def parse_new_side_line_numbers(diff: str) -> dict[str, set[int]]:
    """Return new-side hunk line numbers (added plus context) per changed file.

    Context lines are useful when retrieving the current file for a patch that
    only deletes or replaces a guard. This is deliberately separate from
    :func:`parse_added_lines`, which remains the strict anchor set for findings.
    """
    line_numbers: dict[str, set[int]] = {}
    current_file: str | None = None
    new_line = 0
    in_hunk = False
    for raw in diff.splitlines():
        if raw.startswith("diff --git "):
            current_file, in_hunk = None, False
            continue
        if not in_hunk and raw.startswith("+++ "):
            target = _decode_diff_path(raw[4:].split("\t", 1)[0].strip())
            current_file = None if target == "/dev/null" else target.removeprefix("b/").removeprefix("./")
            continue
        if raw.startswith("--- ") and not in_hunk:
            continue
        hunk = _HUNK_RANGE_RE.match(raw)
        if hunk:
            new_line = int(hunk.group(1))
            in_hunk = True
            if hunk.group(2) == "0" and current_file is not None:
                # Pure deletion: anchor context retrieval near the deletion
                # point in the resulting file even though no new-side hunk
                # lines remain to iterate over.
                line_numbers.setdefault(current_file, set()).add(max(1, new_line))
            continue
        if not in_hunk or current_file is None:
            continue
        if raw.startswith("+"):
            line_numbers.setdefault(current_file, set()).add(new_line)
            new_line += 1
        elif raw.startswith("-") or raw.startswith("\\"):
            continue
        else:
            line_numbers.setdefault(current_file, set()).add(new_line)
            new_line += 1
    return line_numbers


def _decode_diff_path(value: str) -> str:
    if value.startswith('"'):
        try:
            return ast.literal_eval(value)
        except (SyntaxError, ValueError):
            return ""
    return value


def limit_unified_diff(diff: str, max_chars: int) -> tuple[str, bool, list[str]]:
    """Bound a diff without cutting arbitrarily through a file header.

    Complete file patches are preferred. If one file alone exceeds the
    budget, it is cut at a line boundary and an explicit marker is appended.
    The returned omitted-path list is included in the model context so the
    reviewer cannot mistake a partial input for a complete PR diff.
    """
    if len(diff) <= max_chars:
        return diff, False, []

    lines = diff.splitlines(keepends=True)
    chunks: list[str] = []
    current: list[str] = []
    for line in lines:
        if line.startswith("diff --git ") and current:
            chunks.append("".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        chunks.append("".join(current))

    included: list[str] = []
    omitted: list[str] = []
    used = 0
    for index, chunk in enumerate(chunks):
        if used + len(chunk) <= max_chars:
            included.append(chunk)
            used += len(chunk)
            continue
        # Preserve a readable prefix of the first oversized patch, but never
        # append half a line. Subsequent patches are explicitly listed.
        remaining = max_chars - used
        if remaining > 256 and not included:
            prefix = chunk[:remaining].rsplit("\n", 1)[0]
            if prefix:
                included.append(prefix + "\n")
                used += len(prefix) + 1
        omitted.extend(_diff_chunk_path(item) for item in chunks[index:])
        break

    omitted = [path for path in omitted if path]
    marker = "\n[ FIDELIS INPUT LIMIT: remaining diff content omitted. Omitted files: "
    marker += ", ".join(omitted[:50]) if omitted else "remaining diff content"
    marker += " ]\n"
    # Reserve room for the marker. The function remains bounded even when a
    # caller configures an unusually small limit.
    result = "".join(included)
    if len(result) + len(marker) > max_chars:
        result = result[: max(0, max_chars - len(marker))].rsplit("\n", 1)[0]
        marker = marker[: max(0, max_chars - len(result))]
    return result + marker, True, omitted


def _diff_chunk_path(chunk: str) -> str | None:
    for line in chunk.splitlines():
        if line.startswith("+++ "):
            target = _decode_diff_path(line[4:].split("\t", 1)[0].strip())
            return None if target == "/dev/null" else target.removeprefix("b/")
    return None
