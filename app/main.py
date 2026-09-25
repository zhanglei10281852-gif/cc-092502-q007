from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Query

from app.database import close_connection, connection, init_db
from app.schemas import JobCreate, JobFinish, LoginRequest, MemberCreate, ProjectCreate, UserCreate
from app.service import ResearchService, ServiceError


@asynccontextmanager
async def lifespan(app: FastAPI):
    del app
    init_db()
    yield
    close_connection()


app = FastAPI(title="考古研究协作基础服务", version="1.0.0", lifespan=lifespan)


@app.exception_handler(ServiceError)
async def handle_service_error(request, exc: ServiceError):
    del request
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=exc.status, content={"error": {"code": exc.code, "message": exc.message}})


def current_user(authorization: str = Header(...)):
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "缺少 Bearer 会话")
    return ResearchService().authenticate(authorization[7:])


@app.get("/")
def root():
    return {"service": "考古研究协作基础服务", "version": "1.0.0"}


@app.get("/api/system/health")
def health():
    db = connection()
    return {"status": "ok", "foreign_keys": db.execute("PRAGMA foreign_keys").fetchone()[0], "journal_mode": db.execute("PRAGMA journal_mode").fetchone()[0]}


@app.post("/api/users", status_code=201)
def create_user(payload: UserCreate):
    return ResearchService().create_user(payload.model_dump())


@app.post("/api/sessions")
def login(payload: LoginRequest):
    return ResearchService().login(payload.username, payload.password)


@app.post("/api/projects", status_code=201)
def create_project(payload: ProjectCreate, user=Depends(current_user), idempotency_key: str = Header(default="")):
    return ResearchService().create_project(payload.model_dump(), user["id"], idempotency_key)


@app.post("/api/projects/{project_id}/members")
def add_member(project_id: int, payload: MemberCreate, user=Depends(current_user)):
    return ResearchService().add_member(project_id, user["id"], payload.user_id, payload.role)


@app.get("/api/audit")
def list_audit(project_id: int | None = Query(default=None), user=Depends(current_user)):
    service = ResearchService()
    if project_id is not None:
        service.require_role(project_id, user["id"], {"owner", "researcher", "reviewer", "viewer"})
        rows = connection().execute("SELECT * FROM audit_events WHERE project_id=? ORDER BY id", (project_id,)).fetchall()
    else:
        rows = connection().execute("SELECT * FROM audit_events WHERE actor_id=? ORDER BY id", (user["id"],)).fetchall()
    return {"data": [dict(row) for row in rows]}


@app.post("/api/jobs", status_code=202)
def enqueue_job(payload: JobCreate, user=Depends(current_user)):
    return ResearchService().enqueue(payload.model_dump(), user["id"])


@app.post("/api/jobs/claim")
def claim_job(worker_id: str = Query(..., min_length=1)):
    return {"job": ResearchService().claim(worker_id)}


@app.post("/api/jobs/{job_id}/finish")
def finish_job(job_id: int, payload: JobFinish):
    return ResearchService().finish(job_id, payload.worker_id, payload.result)
