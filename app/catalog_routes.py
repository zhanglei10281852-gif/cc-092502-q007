"""遗物编目模块 HTTP 路由。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Query

from app.catalog_schemas import (
    ArtifactCreate, FragmentCreate, LocationCreate, NumberPromote, PackageCreate, ScanBatchCreate,
)
from app.catalog_service import CatalogError, CatalogService
from app.service import ResearchService

router = APIRouter(prefix="/api/projects/{project_id}/catalog", tags=["catalog"])


def current_user(authorization: str = Header(...)):
    if not authorization.startswith("Bearer "):
        raise CatalogError("unauthorized", "缺少 Bearer 会话", 401)
    return ResearchService().authenticate(authorization[7:])


def service() -> CatalogService:
    return CatalogService()


@router.post("/locations", status_code=201)
def create_location(project_id: int, payload: LocationCreate, user=Depends(current_user)):
    return service().register_location(project_id, payload.model_dump(), user["id"])


@router.post("/packages", status_code=201)
def create_package(project_id: int, payload: PackageCreate, user=Depends(current_user)):
    return service().register_package(project_id, payload.model_dump(), user["id"])


@router.post("/artifacts", status_code=201)
def create_artifact(project_id: int, payload: ArtifactCreate, user=Depends(current_user)):
    return service().register_artifact(project_id, payload.model_dump(), user["id"])


@router.get("/artifacts/search")
def search_artifacts(
    project_id: int,
    material: str | None = Query(default=None),
    context_key: str | None = Query(default=None),
    context_value: str | None = Query(default=None),
    q: str | None = Query(default=None, description="编号别名（临时号/正式号）模糊匹配"),
    custody: str | None = Query(default=None, pattern="^(in_stock|loaned_out|missing)$"),
    user=Depends(current_user),
):
    return service().search(project_id, material=material, context_key=context_key,
                            context_value=context_value, query=q, custody=custody, actor_id=user["id"])


@router.get("/artifacts/{artifact_id}")
def get_artifact(project_id: int, artifact_id: int, user=Depends(current_user)):
    svc = service()
    role = svc.membership_role(project_id, user["id"])
    if role is None:
        raise CatalogError("forbidden", "当前用户没有该项目权限", 403)
    return svc._artifact_detail(svc.db, artifact_id, precise=True)


@router.post("/artifacts/{artifact_id}/promote")
def promote_number(project_id: int, artifact_id: int, payload: NumberPromote, user=Depends(current_user)):
    return service().promote_number(project_id, artifact_id, payload.formal_number, user["id"])


@router.post("/fragments", status_code=201)
def create_fragment(project_id: int, payload: FragmentCreate, user=Depends(current_user)):
    return service().register_fragment(project_id, payload.model_dump(), user["id"])


@router.post("/scan-batches", status_code=202)
def preview_batch(project_id: int, payload: ScanBatchCreate, user=Depends(current_user)):
    # 预演：列冲突但不写入任何事件
    return service().preview_batch(project_id, payload.model_dump(), user["id"])


@router.get("/scan-batches/{batch_id}")
def get_batch(project_id: int, batch_id: int, user=Depends(current_user)):
    svc = service()
    batch = svc.get_batch(batch_id)
    if batch["project_id"] != project_id:
        raise CatalogError("batch_not_found", "扫码批次不存在", 404)
    if svc.membership_role(project_id, user["id"]) is None:
        raise CatalogError("forbidden", "当前用户没有该项目权限", 403)
    return batch


@router.post("/scan-batches/{batch_id}/confirm")
def confirm_batch(project_id: int, batch_id: int, user=Depends(current_user)):
    # 先核对批次归属再执行；确认需要 owner/reviewer 角色且不得是提交人本人
    svc = service()
    batch = svc.get_batch(batch_id)
    if batch["project_id"] != project_id:
        raise CatalogError("batch_not_found", "扫码批次不存在", 404)
    return svc.confirm_batch(batch_id, user["id"])


@router.post("/events/{event_id}/compensate")
def compensate_event(project_id: int, event_id: int, effective_at: str | None = Query(default=None),
                     user=Depends(current_user)):
    svc = service()
    row = svc.db.execute("SELECT project_id FROM catalog_events WHERE id=?", (event_id,)).fetchone()
    if row is None:
        raise CatalogError("event_not_found", "事件不存在", 404)
    if row["project_id"] != project_id:
        raise CatalogError("event_not_found", "事件不存在", 404)
    return svc.compensate_event(event_id, user["id"], effective_at)


@router.get("/events")
def list_events(project_id: int, limit: int = Query(default=100, ge=1, le=1000),
                event_type: str | None = Query(default=None), user=Depends(current_user)):
    svc = service()
    if svc.membership_role(project_id, user["id"]) is None:
        raise CatalogError("forbidden", "当前用户没有该项目权限", 403)
    sql = "SELECT * FROM catalog_events WHERE project_id=?"
    params: list = [project_id]
    if event_type:
        sql += " AND event_type=?"
        params.append(event_type)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    import json
    data = []
    for row in svc.db.execute(sql, params):
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json"))
        data.append(item)
    return {"data": data}


@router.get("/state-at")
def state_at(project_id: int, kind: str = Query(..., pattern="^(fragment|package)$"),
             id: int = Query(..., alias="id"), at: str = Query(...), user=Depends(current_user)):
    # 回溯查询：重建指定时间点的状态（精确库位仅对项目成员可见）
    svc = service()
    role = svc.membership_role(project_id, user["id"])
    if role is None:
        raise CatalogError("forbidden", "当前用户没有该项目权限", 403)
    return svc.state_at(project_id, kind, id, at)


@router.get("/verify")
def verify_integrity(project_id: int, user=Depends(current_user)):
    svc = service()
    role = svc.membership_role(project_id, user["id"])
    if role not in {"owner", "reviewer", "researcher"}:
        raise CatalogError("forbidden", "完整性校验需要项目成员权限", 403)
    return svc.verify_integrity(project_id)


@router.get("/stocktake-report")
def stocktake_report(project_id: int, location_code: str | None = Query(default=None),
                     user=Depends(current_user)):
    svc = service()
    role = svc.membership_role(project_id, user["id"])
    if role not in {"owner", "reviewer", "researcher", "recorder"}:
        raise CatalogError("forbidden", "盘点报告需要项目成员权限", 403)
    return svc.stocktake_report(project_id, location_code)
