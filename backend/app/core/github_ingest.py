from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


ISSUE_URL_RE = re.compile(r'^https?://github\.com/([^/]+)/([^/]+)/issues/(\d+)(?:[/?#].*)?$', re.IGNORECASE)


@dataclass
class GitHubIssuePayload:
    owner: str
    repo: str
    issue_number: int
    issue_url: str
    raw_issue: dict[str, Any]
    comments: list[dict[str, Any]]

    @property
    def task_title(self) -> str:
        title = str(self.raw_issue.get('title', '')).strip() or f'Issue #{self.issue_number}'
        return f'[Issue #{self.issue_number}] {title}'

    def to_description(self) -> str:
        issue = self.raw_issue
        labels = [item.get('name', '') for item in issue.get('labels', []) if isinstance(item, dict)]
        lines = [
            f'Issue URL: {self.issue_url}',
            f'Repository: {self.owner}/{self.repo}',
            f'Issue Number: #{self.issue_number}',
            f'State: {issue.get("state", "")}',
            f'Author: {issue.get("user", {}).get("login", "")}',
            f'Labels: {", ".join(label for label in labels if label) or "(none)"}',
            '',
            '## Issue Body',
            (issue.get('body') or '').strip() or '(empty)',
        ]
        if self.comments:
            lines.extend(['', '## Issue Comments'])
            for index, comment in enumerate(self.comments, start=1):
                author = comment.get('user', {}).get('login', '')
                created_at = comment.get('created_at', '')
                body = (comment.get('body') or '').strip() or '(empty)'
                lines.append(f'[{index}] {created_at} by {author}')
                lines.append(body)
                lines.append('')
        return '\n'.join(lines).strip()

    def to_json(self) -> str:
        payload = {
            'owner': self.owner,
            'repo': self.repo,
            'issue_number': self.issue_number,
            'issue_url': self.issue_url,
            'issue': self.raw_issue,
            'comments': self.comments,
        }
        return json.dumps(payload, indent=2, ensure_ascii=False)


def is_github_issue_url(value: str) -> bool:
    return bool(ISSUE_URL_RE.match((value or '').strip()))


def fetch_issue(issue_url: str, *, token: str | None = None, max_comments: int = 10) -> GitHubIssuePayload:
    match = ISSUE_URL_RE.match(issue_url.strip())
    if not match:
        raise ValueError('unsupported GitHub issue URL')
    owner, repo, issue_no_str = match.groups()
    issue_number = int(issue_no_str)
    auth_token = token or os.getenv('GITHUB_TOKEN') or os.getenv('GH_TOKEN') or ''
    issue_api = f'https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}'
    issue = _get_json(issue_api, token=auth_token)
    if issue.get('pull_request'):
        raise ValueError('the provided URL points to a pull request, not an issue')
    comments_url = str(issue.get('comments_url') or '').strip()
    comments: list[dict[str, Any]] = []
    if comments_url:
        comments = _get_json_list(comments_url, token=auth_token, limit=max_comments)
    return GitHubIssuePayload(
        owner=owner,
        repo=repo,
        issue_number=issue_number,
        issue_url=issue_url,
        raw_issue=issue,
        comments=comments,
    )


def _get_json(url: str, *, token: str = '') -> dict[str, Any]:
    request = urllib.request.Request(url, headers=_headers(token))
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors='ignore')
        raise RuntimeError(f'GitHub API request failed ({exc.code}): {body[:300]}') from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f'GitHub API request failed: {exc.reason}') from exc


def _get_json_list(url: str, *, token: str = '', limit: int = 10) -> list[dict[str, Any]]:
    request = urllib.request.Request(url, headers=_headers(token))
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors='ignore')
        raise RuntimeError(f'GitHub comments request failed ({exc.code}): {body[:300]}') from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f'GitHub comments request failed: {exc.reason}') from exc
    if not isinstance(data, list):
        return []
    items = [item for item in data if isinstance(item, dict)]
    return items[:limit] if limit > 0 else items


def _headers(token: str) -> dict[str, str]:
    headers = {
        'Accept': 'application/vnd.github+json',
        'User-Agent': 'agent-mission-control',
    }
    if token:
        headers['Authorization'] = f'Bearer {token}'
    return headers
