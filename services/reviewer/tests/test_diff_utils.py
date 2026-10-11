from app.reviewers.diff_utils import AddedLine, parse_added_lines

from tests.conftest import SAMPLE_DIFF


def test_parses_added_lines_with_new_file_numbers():
    added = parse_added_lines(SAMPLE_DIFF)
    assert added[:2] == [
        AddedLine("app/users.py", 11, "    new_a = 2"),
        AddedLine("app/users.py", 12, "    new_b = 3"),
    ]
    assert [(a.file, a.line) for a in added[2:]] == [
        ("app/db.py", 1), ("app/db.py", 2), ("app/db.py", 3),
    ]


def test_removed_lines_do_not_advance_new_numbering():
    diff = (
        "--- a/x.py\n+++ b/x.py\n@@ -1,4 +1,4 @@\n"
        " keep\n-old\n+new\n keep2\n"
    )
    assert parse_added_lines(diff) == [AddedLine("x.py", 2, "new")]


def test_deleted_files_and_no_newline_marker_are_ignored():
    diff = (
        "diff --git a/gone.py b/gone.py\n--- a/gone.py\n+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n-a\n-b\n"
        "diff --git a/n.py b/n.py\n--- a/n.py\n+++ b/n.py\n@@ -1 +1 @@\n"
        "-x\n+y\n\\ No newline at end of file\n"
    )
    assert parse_added_lines(diff) == [AddedLine("n.py", 1, "y")]


def test_multiple_hunks_in_one_file():
    diff = (
        "--- a/m.py\n+++ b/m.py\n"
        "@@ -1,1 +1,2 @@\n a\n+b\n"
        "@@ -10,1 +11,2 @@\n j\n+k\n"
    )
    assert [(a.line, a.text) for a in parse_added_lines(diff)] == [(2, "b"), (12, "k")]


def test_plus_plus_plus_inside_hunk_is_content_not_header():
    diff = "--- a/f.txt\n+++ b/f.txt\n@@ -1 +1,2 @@\n a\n+++ looks like a header\n"
    assert parse_added_lines(diff) == [AddedLine("f.txt", 2, "++ looks like a header")]


def test_garbage_input_yields_nothing():
    assert parse_added_lines("") == []
    assert parse_added_lines("not a diff at all\n+ but has plus\n") == []
