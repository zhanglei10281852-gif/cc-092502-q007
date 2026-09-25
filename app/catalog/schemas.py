from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class LocationCreate(BaseModel):
    code: str = Field(..., min_length=1, max_length=60)
    area: str = Field(..., min_length=1, max_length=60)
    kind: Literal["storage", "external", "transit"] = "storage"


class PackageCreate(BaseModel):
    code: str = Field(..., min_length=1, max_length=60)


class ArtifactCreate(BaseModel):
    number: str = Field(..., min_length=1, max_length=80)
    number_kind: Literal["temporary", "formal"] = "temporary"
    material: str = Field(..., min_length=1, max_length=40)
    context: str = Field(default="", max_length=120)


class NumberAssign(BaseModel):
    number: str = Field(..., min_length=1, max_length=80)
    kind: Literal["temporary", "formal"] = "formal"


class FragmentCreate(BaseModel):
    code: str = Field(..., min_length=1, max_length=60)
    package_code: str = Field(..., min_length=1, max_length=60)
    location_code: str = Field(..., min_length=1, max_length=60)
    note: str = Field(default="", max_length=200)


class FragmentJoin(BaseModel):
    artifact_id: int


class BatchItem(BaseModel):
    fragment_code: str | None = Field(default=None, max_length=60)
    package_code: str | None = Field(default=None, max_length=60)
    to_package_code: str | None = Field(default=None, max_length=60)


class BatchSubmit(BaseModel):
    batch_key: str = Field(..., min_length=1, max_length=120)
    operation: Literal["move", "loan_out", "return", "split", "merge", "inventory"]
    occurred_at: str | None = None
    location_code: str | None = Field(default=None, max_length=60)
    from_package_code: str | None = Field(default=None, max_length=60)
    to_package_code: str | None = Field(default=None, max_length=60)
    items: list[BatchItem] = Field(default_factory=list)
    note: str = Field(default="", max_length=200)


class EventReverse(BaseModel):
    note: str = Field(default="", max_length=200)
