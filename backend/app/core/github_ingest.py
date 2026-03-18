from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


PULL_REQUEST_URL_RE = re.compile(r'^https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)(?:[/?#].*)?$', re.IGNORECASE)


@dataclass
class GitHubPullRequestPayload:
    owner: str
    repo: str
    pr_number: int
    pr_url: str
    raw_pr: dict[str, Any]
    issue_comments: list[dict[str, Any]]
    review_comments: list[dict[str, Any]]
    reviews: list[dict[str, Any]]
    files: list[dict[str, Any]]

    @property
    def task_title(self) -> str:
        title = str(self.raw_pr.get('title', '')).strip() or f'PR #{self.pr_number}'
        return f'[PR #{self.pr_number}] {title}'

    @property
    def changed_files(self) -> list[str]:
        items: list[str] = []
        for item in self.files:
            filename = str(item.get('filename', '')).strip()
            if filename and filename not in items:
                items.append(filename)
        return items

    def to_description(self, review_focus: str = '') -> str:
        pr = self.raw_pr
        lines = [
            f'PR URL: {self.pr_url}',
            f'Repository: {self.owner}/{self.repo}',
            f'PR Number: #{self.pr_number}',
            f'State: {pr.get("state", "")}',
            f'Author: {pr.get("user", {}).get("login", "")}',
            f'Base: {pr.get("base", {}).get("ref", "")}',
            f'Head: {pr.get("head", {}).get("ref", "")}',
            f'Changed Files: {len(self.changed_files)}',
            '',
            '## Review Goal',
            'Review this pull request for bugs, regressions, risky assumptions, and missing tests.',
        ]
        if review_focus.strip():
            lines.extend(['', '## Review Focus', review_focus.strip()])
        lines.extend([
            '',
            '## Pull Request Body',
            (pr.get('body') or '').strip() or '(empty)',
        ])
        lines.extend(['', '## Changed Files'])
        if self.files:
            for item in self.files:
                filename = item.get('filename', '')
                additions = item.get('additions', 0)
                deletions = item.get('deletions', 0)
                status = item.get('status', '')
                lines.append(f'- {filename} [{status}] (+{additions} -{deletions})')
        else:
            lines.append('- (none)')
        if self.reviews:
            lines.extend(['', '## Existing Reviews'])
            for index, review in enumerate(self.reviews, start=1):
                author = review.get('user', {}).get('login', '')
                state = review.get('state', '')
                submitted_at = review.get('submitted_at', '')
                body = (review.get('body') or '').strip() or '(empty)'
                lines.append(f'[{index}] {submitted_at} by {author} [{state}]')
                lines.append(body)
                lines.append('')
        if self.issue_comments:
            lines.extend(['', '## PR Conversation'])
            for index, comment in enumerate(self.issue_comments, start=1):
                author = comment.get('user', {}).get('login', '')
                created_at = comment.get('created_at', '')
                body = (comment.get('body') or '').strip() or '(empty)'
                lines.append(f'[{index}] {created_at} by {author}')
                lines.append(body)
                lines.append('')
        if self.review_comments:
            lines.extend(['', '## Inline Review Comments'])
            for index, comment in enumerate(self.review_comments, start=1):
                author = comment.get('user', {}).get('login', '')
                path = comment.get('path', '')
                line = comment.get('line') or comment.get('original_line') or '?'
                body = (comment.get('body') or '').strip() or '(empty)'
                lines.append(f'[{index}] {path}:{line} by {author}')
                lines.append(body)
                lines.append('')
        return '\n'.join(lines).strip()

    def to_json(self) -> str:
        payload = {
            'owner': self.owner,
            'repo': self.repo,
            'pr_number': self.pr_number,
            'pr_url': self.pr_url,
            'pull_request': self.raw_pr,
            'reviews': self.reviews,
            'issue_comments': self.issue_comments,
            'review_comments': self.review_comments,
            'files': self.files,
        }
        return json.dumps(payload, indent=2, ensure_ascii=False)


def is_github_pr_url(value: str) -> bool:
    return bool(PULL_REQUEST_URL_RE.match((value or '').strip()))


def fetch_pull_request(pr_url: str, *, token: str | None = None, max_items: int = 20, max_files: int = 100) -> GitHubPullRequestPayload:
    match = PULL_REQUEST_URL_RE.match(pr_url.strip())
    if not match:
        raise ValueError('unsupported GitHub pull request URL')
    owner, repo, pr_no_str = match.groups()
    pr_number = int(pr_no_str)
    auth_token = token or os.getenv('GITHUB_TOKEN') or os.getenv('GH_TOKEN') or ''
    pr_api = f'https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}'
    issue_comments_api = f'https://api.github.com/repos/{owner}/{repo}/issues/{pr_number}/comments'
    reviews_api = f'https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/reviews'
    review_comments_api = f'https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/comments'
    files_api = f'https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/files'
    pull_request = _get_json(pr_api, token=auth_token)
    issue_comments = _get_json_list(issue_comments_api, token=auth_token, limit=max_items)
    reviews = _get_json_list(reviews_api, token=auth_token, limit=max_items)
    review_comments = _get_json_list(review_comments_api, token=auth_token, limit=max_items)
    files = _get_json_list(files_api, token=auth_token, limit=max_files)
    return GitHubPullRequestPayload(
        owner=owner,
        repo=repo,
        pr_number=pr_number,
        pr_url=pr_url,
        raw_pr=pull_request,
        issue_comments=issue_comments,
        review_comments=review_comments,
        reviews=reviews,
        files=files,
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


def _get_json_list(url: str, *, token: str = '', limit: int = 20) -> list[dict[str, Any]]:
    request = urllib.request.Request(_with_per_page(url, limit), headers=_headers(token))
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors='ignore')
        raise RuntimeError(f'GitHub list request failed ({exc.code}): {body[:300]}') from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f'GitHub list request failed: {exc.reason}') from exc
    if not isinstance(data, list):
        return []
    items = [item for item in data if isinstance(item, dict)]
    return items[:limit] if limit > 0 else items


def _with_per_page(url: str, limit: int) -> str:
    if limit <= 0:
        return url
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    query['per_page'] = [str(min(limit, 100))]
    return parsed._replace(query=urllib.parse.urlencode(query, doseq=True)).geturl()


def _headers(token: str) -> dict[str, str]:
    headers = {
        'Accept': 'application/vnd.github+json',
        'User-Agent': 'review-control',
    }
    if token:
        headers['Authorization'] = f'Bearer {token}'
    return headers
