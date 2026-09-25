from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_path: Path
    token_secret: str


def settings() -> Settings:
    return Settings(
        database_path=Path(os.getenv("ARCHAEOLOGY_DATABASE_PATH", "./data/archaeology.db")),
        token_secret=os.getenv("ARCHAEOLOGY_TOKEN_SECRET", "development-secret"),
    )
