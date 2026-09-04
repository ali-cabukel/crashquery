from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.environ.get("CRASHQUERY_DATA_DIR", PROJECT_ROOT / "data"))
RAW_DIR = DATA_DIR / "raw"


@dataclass(frozen=True)
class Settings:
    agent_dsn: str = os.environ.get(
        "AGENT_DSN", "postgresql://rsa_agent:rsa_agent_pw@localhost:5433/road_safety"
    )
    owner_dsn: str = os.environ.get(
        "OWNER_DSN", "postgresql://rsa_owner:rsa_owner_pw@localhost:5433/road_safety"
    )
    model: str = os.environ.get("AGENT_MODEL", "anthropic:claude-sonnet-4-5")
    statement_timeout_ms: int = int(os.environ.get("STATEMENT_TIMEOUT_MS", "30000"))
    max_iterations: int = int(os.environ.get("AGENT_MAX_ITERATIONS", "25"))
    raw_dir: Path = RAW_DIR


def get_settings() -> Settings:
    return Settings()
