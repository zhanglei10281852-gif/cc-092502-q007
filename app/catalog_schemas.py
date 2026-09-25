"""遗物编目模块的请求/响应模型。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class LocationCreate(BaseModel):
    code: str = Field(..., min_length=1, max_length=60)
    area_code: str = Field("", max_length=40)
    position: str = Field("", max_length=120)


class PackageCreate(BaseModel):
    code: str = Field(..., min_length=1, max_length=60)
    package_type: str = Field("box", max_length=40)
    initial_location_code: str | None = Field(None, max_length=60)


class ArtifactCreate(BaseModel):
    temporary_number: str = Field(..., min_length=1, max_length=60)
    material: Literal["wood", "rope", "textile", "other"]
    context: dict[str, Any] = Field(default_factory=dict)


class NumberPromote(BaseModel):
    formal_number: str = Field(..., min_length=1, max_length=60)


class FragmentCreate(BaseModel):
    artifact_ref: str = Field(..., description="遗物编号或别名（临时号/正式号）")
    label: str = Field(..., min_length=1, max_length=60, description="片段标签，扫码码为 编号#标签")
    package_code: str = Field(..., min_length=1, max_length=60)


class _BatchAction(BaseModel):
    effective_at: str | None = None


class TransferAction(_BatchAction):
    type: Literal["transfer"]
    package_code: str
    to_location_code: str
    expected_version: int | None = None


class JoinAction(_BatchAction):
    """合包/装盒：把若干片段放入指定包装。"""
    type: Literal["join"]
    fragment_codes: list[str] = Field(..., min_length=1)
    to_package_code: str


class SplitAction(_BatchAction):
    """分装：一个片段拆成多个子片段，分别入盒。"""
    type: Literal["split"]
    fragment_code: str
    children: list[SplitChild] = Field(..., min_length=2)


class SplitChild(BaseModel):
    label: str = Field(..., min_length=1, max_length=60)
    package_code: str


class MergeAction(_BatchAction):
    """清理后拼合：多个存量片段合并为一个（目标可为既有片段或新标签）。"""
    type: Literal["merge"]
    fragment_codes: list[str] = Field(..., min_length=2)
    target_fragment_code: str | None = None
    target_label: str | None = Field(None, max_length=60)
    target_package_code: str


class LoanOutAction(_BatchAction):
    type: Literal["loan_out"]
    fragment_codes: list[str] = Field(..., min_length=1)
    loan_ref: str = Field(..., min_length=1, max_length=80)
    loan_to: str = Field(..., min_length=1, max_length=120)


class LoanReturnAction(_BatchAction):
    type: Literal["loan_return"]
    fragment_codes: list[str] = Field(..., min_length=1)
    loan_ref: str = Field(..., min_length=1, max_length=80)


class StocktakeAction(_BatchAction):
    type: Literal["stocktake"]
    location_code: str
    observed_fragment_codes: list[str] = Field(default_factory=list)


class ScanBatchCreate(BaseModel):
    batch_key: str = Field(..., min_length=1, max_length=120)
    effective_at: str | None = None
    actions: list[dict[str, Any]] = Field(..., min_length=1)
