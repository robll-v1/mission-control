"""Unit tests for the unified-diff parser that feeds review prompts.

Regression tests for two silent drops: patches for deleted files and for
paths containing spaces (git terminates those header paths with a tab).
"""
from __future__ import annotations

from app.core.context_compiler import ContextCompiler

SPACE_FILE_SECTION = (
    'diff --git a/my file with space.txt b/my file with space.txt\n'
    'index 1111111..2222222 100644\n'
    '--- a/my file with space.txt\t\n'
    '+++ b/my file with space.txt\t\n'
    '@@ -1 +1,2 @@\n'
    ' hello\n'
    '+world\n'
)

DELETED_FILE_SECTION = (
    'diff --git a/deleted.txt b/deleted.txt\n'
    'deleted file mode 100644\n'
    'index 3333333..0000000\n'
    '--- a/deleted.txt\n'
    '+++ /dev/null\n'
    '@@ -1,2 +0,0 @@\n'
    '-old line 1\n'
    '-old line 2\n'
)

PLAIN_FILE_SECTION = (
    'diff --git a/keep.txt b/keep.txt\n'
    'index 4444444..5555555 100644\n'
    '--- a/keep.txt\n'
    '+++ b/keep.txt\n'
    '@@ -1 +1,2 @@\n'
    ' keep\n'
    '+more\n'
)

RENAME_SECTION = (
    'diff --git a/old name.txt b/new name.txt\n'
    'similarity index 90%\n'
    'rename from old name.txt\n'
    'rename to new name.txt\n'
    '--- a/old name.txt\t\n'
    '+++ b/new name.txt\t\n'
    '@@ -1 +1 @@\n'
    '-one\n'
    '+two\n'
)


def _parse(sections, review_paths):
    return ContextCompiler._parse_unified_diff('\n'.join(sections), review_paths)


def test_space_path_patch_is_kept():
    patches = _parse(
        [SPACE_FILE_SECTION, PLAIN_FILE_SECTION],
        ['my file with space.txt', 'keep.txt'],
    )
    by_path = {item['path']: item for item in patches}
    assert set(by_path) == {'my file with space.txt', 'keep.txt'}
    assert '+world' in by_path['my file with space.txt']['patch']
    assert by_path['my file with space.txt']['status'] == 'modified'


def test_deleted_file_patch_is_kept_with_deletions():
    patches = _parse(
        [DELETED_FILE_SECTION, PLAIN_FILE_SECTION],
        ['deleted.txt', 'keep.txt'],
    )
    by_path = {item['path']: item for item in patches}
    deleted = by_path['deleted.txt']
    assert deleted['status'] == 'deleted'
    assert '-old line 1' in deleted['patch']
    assert deleted['deletions'] == 2
    assert deleted['additions'] == 0


def test_rename_reports_new_path():
    patches = _parse([RENAME_SECTION], ['new name.txt'])
    assert [item['path'] for item in patches] == ['new name.txt']
    assert '+two' in patches[0]['patch']


def test_allowed_paths_filter_still_applies():
    assert _parse([PLAIN_FILE_SECTION], ['other.txt']) == []
    # No filter means every section is kept, including deletions.
    unfiltered = _parse([DELETED_FILE_SECTION], [])
    assert [item['path'] for item in unfiltered] == ['deleted.txt']
