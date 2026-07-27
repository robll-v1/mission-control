from typing import Literal

from pydantic import BaseModel


class CreateTaskRequest(BaseModel):
    repo_path: str
    source_type: Literal['pull_request', 'local_diff'] = 'pull_request'
    pr_url: str | None = None
    base: str | None = None
    review_focus: str = ''
    backend: str = 'opencode'
