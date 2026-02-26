"""Tests for git diff feature."""

from claudechic.features.diff.git import (
    _parse_name_status,
    _parse_hunks,
    _merge_diff_content,
    FileChange,
)


class TestParseNameStatus:
    def test_modified_file(self):
        output = "M\tpath/to/file.py"
        changes = _parse_name_status(output)
        assert len(changes) == 1
        assert changes[0].path == "path/to/file.py"
        assert changes[0].status == "modified"

    def test_added_file(self):
        output = "A\tnew_file.py"
        changes = _parse_name_status(output)
        assert len(changes) == 1
        assert changes[0].status == "added"

    def test_deleted_file(self):
        output = "D\told_file.py"
        changes = _parse_name_status(output)
        assert len(changes) == 1
        assert changes[0].status == "deleted"

    def test_multiple_files(self):
        output = "M\tfile1.py\nA\tfile2.py\nD\tfile3.py"
        changes = _parse_name_status(output)
        assert len(changes) == 3

    def test_empty_output(self):
        changes = _parse_name_status("")
        assert len(changes) == 0


class TestParseHunks:
    def test_single_hunk(self):
        diff_section = """a/file.py b/file.py
index abc123..def456 100644
--- a/file.py
+++ b/file.py
@@ -1,3 +1,4 @@
 line1
 line2
+new line
 line3
"""
        hunks = _parse_hunks(diff_section)
        assert len(hunks) == 1
        assert hunks[0].old_start == 1
        assert hunks[0].old_count == 3
        assert hunks[0].new_start == 1
        assert hunks[0].new_count == 4
        assert "new line" in hunks[0].new_lines
        assert "new line" not in hunks[0].old_lines

    def test_multiple_hunks(self):
        diff_section = """a/file.py b/file.py
@@ -1,3 +1,4 @@
 line1
+added1
 line2
 line3
@@ -10,3 +11,4 @@
 line10
+added2
 line11
 line12
"""
        hunks = _parse_hunks(diff_section)
        assert len(hunks) == 2
        assert hunks[0].old_start == 1
        assert hunks[1].old_start == 10
        assert "added1" in hunks[0].new_lines
        assert "added2" in hunks[1].new_lines

    def test_deletion_hunk(self):
        diff_section = """a/file.py b/file.py
@@ -1,4 +1,3 @@
 line1
-deleted line
 line2
 line3
"""
        hunks = _parse_hunks(diff_section)
        assert len(hunks) == 1
        assert "deleted line" in hunks[0].old_lines
        assert "deleted line" not in hunks[0].new_lines


class TestMergeDiffContent:
    def test_merges_hunks_to_files(self):
        """Use a path containing 'b/' to also cover regression #54."""
        files = [
            FileChange(path="bokehjs/src/lib/patch.ts", status="modified", hunks=[]),
        ]
        diff_text = """diff --git a/bokehjs/src/lib/patch.ts b/bokehjs/src/lib/patch.ts
index abc..def 100644
--- a/bokehjs/src/lib/patch.ts
+++ b/bokehjs/src/lib/patch.ts
@@ -1,2 +1,3 @@
 old
+new
 end
"""
        result = _merge_diff_content(files, diff_text)
        assert len(result) == 1
        assert len(result[0].hunks) == 1
        assert "new" in result[0].hunks[0].new_lines
