from __future__ import annotations

from fastapi import Header, HTTPException

from app.service import ResearchService


def current_user(authorization: str = Header(...)):
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "缺少 Bearer 会话")
    return ResearchService().authenticate(authorization[7:])
