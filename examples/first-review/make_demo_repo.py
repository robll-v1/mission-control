#!/usr/bin/env python3
"""Create a tiny synthetic repo for the first-review demo.

The demo repo contains one real, explainable bug: ``search()`` is rewritten
to build its SQL with an f-string, so a caller-supplied keyword can inject
SQL. Everything is synthetic — no credentials, no network, no private code.

Usage:
    python3 make_demo_repo.py [TARGET_DIR]     # create repo with the bug
    python3 make_demo_repo.py TARGET --apply-fix   # apply the fix (still uncommitted)

Then, from anywhere:
    amc review --repo TARGET --backend opencode --format json
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

SAFE_NOTES = '''"""A tiny note store backed by sqlite3."""
import sqlite3
import sys


class NoteStore:
    def __init__(self, path: str = "notes.db"):
        self.conn = sqlite3.connect(path)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, title TEXT, body TEXT)"
        )
        self.conn.commit()

    def add(self, title: str, body: str) -> int:
        cursor = self.conn.execute(
            "INSERT INTO notes (title, body) VALUES (?, ?)", (title, body)
        )
        self.conn.commit()
        return cursor.lastrowid

    def search(self, keyword: str):
        # Parameterized: the keyword is bound, never interpreted as SQL.
        cursor = self.conn.execute(
            "SELECT id, title, body FROM notes WHERE title LIKE ?", (f"%{keyword}%",)
        )
        return cursor.fetchall()


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    store = NoteStore()
    if len(argv) < 2:
        print("usage: notes.py add TITLE BODY | notes.py find KEYWORD")
        return 2
    command, arg = argv[0], argv[1]
    if command == "add":
        note_id = store.add(arg, argv[2] if len(argv) > 2 else "")
        print(f"added note {note_id}")
    elif command == "find":
        for row in store.search(arg):
            print(row)
    else:
        print(f"unknown command: {command}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

BUGGY_NOTES = SAFE_NOTES.replace(
    '''    def search(self, keyword: str):
        # Parameterized: the keyword is bound, never interpreted as SQL.
        cursor = self.conn.execute(
            "SELECT id, title, body FROM notes WHERE title LIKE ?", (f"%{keyword}%",)
        )
        return cursor.fetchall()''',
    '''    def search(self, keyword: str):
        # Interpolating the keyword directly is shorter than binding it.
        cursor = self.conn.execute(
            f"SELECT id, title, body FROM notes WHERE title LIKE '%{keyword}%'"
        )
        return cursor.fetchall()''',
)


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", nargs="?", default="amc-demo-notes", help="directory to create (must not exist)")
    parser.add_argument("--apply-fix", action="store_true", help="restore the parameterized query in an existing demo repo")
    args = parser.parse_args()

    repo = Path(args.target).resolve()

    if args.apply_fix:
        notes = repo / "notes.py"
        if not notes.exists():
            print(f"error: {notes} not found", file=sys.stderr)
            return 1
        notes.write_text(SAFE_NOTES, encoding="utf-8")
        print(f"Fix applied in {repo} (uncommitted). Re-run: amc review --repo {repo}")
        return 0

    if repo.exists():
        print(f"error: {repo} already exists", file=sys.stderr)
        return 1

    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True)
    git(repo, "config", "user.name", "Demo User")
    git(repo, "config", "user.email", "demo@example.com")
    (repo / "notes.py").write_text(SAFE_NOTES, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "Add note store with parameterized queries")
    (repo / "notes.py").write_text(BUGGY_NOTES, encoding="utf-8")  # uncommitted change

    print(f"Demo repo ready at {repo}")
    print(f"HEAD is the safe base; the working tree adds an SQL-injection bug in notes.py:search().")
    print(f"\nFirst review:")
    print(f"  amc review --repo {repo} --backend opencode --format json")
    print(f"\nAfter reviewing, apply the fix and re-review:")
    print(f"  python3 {Path(__file__).resolve()} {repo} --apply-fix")
    print(f"  amc review --repo {repo} --backend opencode --format json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
