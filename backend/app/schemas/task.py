from pydantic import BaseModel


class CreateTaskRequest(BaseModel):
    repo_path: str
    pr_url: str
    review_focus: str = ''
    backend: str = 'opencode'
