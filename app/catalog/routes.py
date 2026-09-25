from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Query, Response

from app.catalog.report import build_inventory_report
from app.catalog.schemas import (
    ArtifactCreate,
    BatchSubmit,
    EventReverse,
    FragmentCreate,
    FragmentJoin,
    LocationCreate,
    NumberAssign,
    PackageCreate,
)
from app.catalog.service import CatalogService
from app.database import connection
from app.deps import current_user

router = APIRouter(prefix="/api/projects/{project_id}/catalog", tags=["catalog"])


@router.post("/locations", status_code=201)
def create_location(project_id: int, payload: LocationCreate, user=Depends(current_user)):
    return CatalogService().create_location(project_id, payload.model_dump(), user)


@router.get("/locations")
def list_locations(project_id: int, user=Depends(current_user)):
    return {"data": CatalogService().list_locations(project_id, user)}


@router.post("/packages", status_code=201)
def create_package(project_id: int, payload: PackageCreate, user=Depends(current_user)):
    return CatalogService().create_package(project_id, payload.model_dump(), user)


@router.get("/packages")
def list_packages(project_id: int, user=Depends(current_user)):
    return {"data": CatalogService().list_packages(project_id, user)}


@router.post("/artifacts", status_code=201)
def register_artifact(project_id: int, payload: ArtifactCreate, user=Depends(current_user), idempotency_key: str = Header(default="")):
    return CatalogService().register_artifact(project_id, payload.model_dump(), user, idempotency_key)


@router.get("/artifacts/{artifact_id}")
def get_artifact(project_id: int, artifact_id: int, user=Depends(current_user)):
    return CatalogService().get_artifact(project_id, artifact_id, user)


@router.post("/artifacts/{artifact_id}/number")
def assign_number(project_id: int, artifact_id: int, payload: NumberAssign, user=Depends(current_user)):
    return CatalogService().assign_number(project_id, artifact_id, payload.model_dump(), user)


@router.post("/artifacts/{artifact_id}/fragments", status_code=201)
def register_fragment(project_id: int, artifact_id: int, payload: FragmentCreate, user=Depends(current_user)):
    return CatalogService().register_fragment(project_id, artifact_id, payload.model_dump(), user)


@router.get("/fragments")
def search_fragments(
    project_id: int,
    material: str | None = Query(default=None),
    context: str | None = Query(default=None),
    number: str | None = Query(default=None),
    custody: str | None = Query(default=None),
    package_code: str | None = Query(default=None),
    location_code: str | None = Query(default=None),
    limit: int = Query(default=200, le=1000),
    user=Depends(current_user),
):
    filters = {"material": material, "context": context, "number": number, "custody": custody, "package_code": package_code, "location_code": location_code, "limit": limit}
    return {"data": CatalogService().search_fragments(project_id, filters, user)}


@router.get("/fragments/{fragment_id}")
def get_fragment(project_id: int, fragment_id: int, user=Depends(current_user)):
    return CatalogService().get_fragment(project_id, fragment_id, user)


@router.post("/fragments/{fragment_id}/join")
def join_fragment(project_id: int, fragment_id: int, payload: FragmentJoin, user=Depends(current_user)):
    return CatalogService().join_fragment(project_id, fragment_id, payload.model_dump(), user)


@router.get("/fragments/{fragment_id}/history")
def fragment_history(project_id: int, fragment_id: int, user=Depends(current_user)):
    return {"data": CatalogService().fragment_history(project_id, fragment_id, user)}


@router.get("/fragments/{fragment_id}/state")
def fragment_state_at(project_id: int, fragment_id: int, at: str | None = Query(default=None), user=Depends(current_user)):
    return CatalogService().fragment_state_at(project_id, fragment_id, at, user)


@router.get("/state")
def state_at(project_id: int, at: str | None = Query(default=None), user=Depends(current_user)):
    return CatalogService().state_at(project_id, at, user)


@router.get("/events")
def list_events(project_id: int, event_type: str | None = Query(default=None), limit: int = Query(default=200, le=1000), user=Depends(current_user)):
    return {"data": CatalogService().list_events(project_id, user, event_type=event_type, limit=limit)}


@router.get("/events/{event_id}")
def get_event(project_id: int, event_id: int, user=Depends(current_user)):
    return CatalogService().get_event(project_id, event_id, user)


@router.post("/events/{event_id}/reverse", status_code=201)
def reverse_event(project_id: int, event_id: int, payload: EventReverse, user=Depends(current_user)):
    return CatalogService().reverse_event(project_id, event_id, payload.model_dump(), user)


@router.post("/batches")
def submit_batch(project_id: int, payload: BatchSubmit, response: Response, user=Depends(current_user)):
    view, created = CatalogService().submit_batch(project_id, payload.model_dump(), user)
    response.status_code = 201 if created else 200
    return view


@router.get("/batches")
def list_batches(project_id: int, status: str | None = Query(default=None), user=Depends(current_user)):
    return {"data": CatalogService().list_batches(project_id, user, status=status)}


@router.get("/batches/{batch_id}")
def get_batch(project_id: int, batch_id: int, user=Depends(current_user)):
    return CatalogService().get_batch(project_id, batch_id, user)


@router.post("/batches/{batch_id}/confirm")
def confirm_batch(project_id: int, batch_id: int, user=Depends(current_user)):
    return CatalogService().confirm_batch(project_id, batch_id, user)


@router.post("/batches/{batch_id}/reject")
def reject_batch(project_id: int, batch_id: int, user=Depends(current_user)):
    return CatalogService().reject_batch(project_id, batch_id, user)


@router.get("/verification")
def verification(project_id: int, user=Depends(current_user)):
    return CatalogService().verification(project_id, user)


@router.get("/report")
def inventory_report(project_id: int, at: str | None = Query(default=None), user=Depends(current_user)):
    CatalogService()._role(project_id, user["id"], {"owner", "researcher", "recorder", "reviewer", "viewer"})
    project = connection().execute("SELECT code FROM projects WHERE id=?", (project_id,)).fetchone()
    return build_inventory_report(connection(), project["code"], at)
