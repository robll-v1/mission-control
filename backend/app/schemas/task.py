from typing import Literal

from pydantic import BaseModel, field_validator

from app.core.context_compiler import normalize_review_language


class CreateTaskRequest(BaseModel):
    repo_path: str
    source_type: Literal['pull_request', 'local_diff'] = 'pull_request'
    pr_url: str | None = None
    base: str | None = None
    review_focus: str = ''
    backend: str | None = None
    model: str | None = None
    reasoning: str | None = None
    language: str | None = None

    @field_validator('language')
    @classmethod
    def _validate_language(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_review_language(value, strict=True)
