from __future__ import annotations

from pydantic import BaseModel, Field


class ProjectCreate(BaseModel):
    code: str = Field(..., min_length=2, max_length=40)
    name: str = Field(..., min_length=1, max_length=120)
    site_name: str = Field(..., min_length=1, max_length=120)


class UserCreate(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    display_name: str = Field(..., min_length=1, max_length=80)
    password: str = Field(..., min_length=10, max_length=200)


class LoginRequest(BaseModel):
    username: str
    password: str


class MemberCreate(BaseModel):
    user_id: int
    role: str = Field(..., pattern="^(owner|researcher|recorder|reviewer|viewer)$")


class JobCreate(BaseModel):
    project_id: int | None = None
    job_type: str = Field(..., min_length=1, max_length=80)
    job_key: str = Field(..., min_length=1, max_length=160)
    input: dict = Field(default_factory=dict)


class JobFinish(BaseModel):
    worker_id: str = Field(..., min_length=1, max_length=80)
    result: dict = Field(default_factory=dict)
