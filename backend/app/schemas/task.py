from pydantic import BaseModel


class CreateTaskRequest(BaseModel):
    title: str = ''
    repo_path: str
    description: str = ''
    source_type: str = 'manual'
    source_url: str | None = None
    backend: str = 'opencode'
