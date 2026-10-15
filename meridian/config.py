from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_dotenv() -> None:
    env_path = ROOT / ".env"
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"\'')
        if key:
            os.environ.setdefault(key, value)


_load_dotenv()


def _path(name: str, default: str) -> Path:
    value = Path(os.getenv(name, default))
    return value if value.is_absolute() else ROOT / value


def _command(name: str, default: str) -> str:
    value = os.getenv(name, default)
    path = Path(value)
    if not path.is_absolute():
        project_path = ROOT / path
        if project_path.exists():
            return str(project_path)
    return value


@dataclass(frozen=True)
class Settings:
    db_path: Path = _path("MERIDIAN_DB", "data/meridian.sqlite3")
    universe_path: Path = _path("MERIDIAN_UNIVERSE", "data/fortune500.csv")
    upload_dir: Path = _path("MERIDIAN_UPLOAD_DIR", "uploads")
    sec_user_agent: str = os.getenv("SEC_USER_AGENT", "")
    sec_min_interval: float = float(os.getenv("SEC_MIN_REQUEST_INTERVAL", "0.12"))
    retrieval_limit: int = int(os.getenv("RETRIEVAL_LIMIT", "8"))
    max_upload_mb: int = int(os.getenv("MAX_UPLOAD_MB", "40"))
    llm_base_url: str = os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
    llm_api_key: str = os.getenv("LLM_API_KEY", "")
    llm_model: str = os.getenv("LLM_MODEL", "openai/gpt-oss-20b")
    claim_judge_enabled: bool = os.getenv("CLAIM_JUDGE_ENABLED", "false").lower() in {"1", "true", "yes"}
    vision_enabled: bool = os.getenv("LLM_VISION_ENABLED", "false").lower() in {"1", "true", "yes"}
    tesseract_cmd: str = _command("TESSERACT_CMD", "tools/tesseract/tesseract.exe" if (ROOT / "tools/tesseract/tesseract.exe").exists() else "tesseract")
    pdftoppm_cmd: str = os.getenv("PDFTOPPM_CMD", "pdftoppm")
    embedding_base_url: str = os.getenv("EMBEDDING_BASE_URL", "").rstrip("/")
    embedding_api_key: str = os.getenv("EMBEDDING_API_KEY", "")
    embedding_provider: str = os.getenv("EMBEDDING_PROVIDER", "sentence-transformers").strip().lower()
    embedding_model: str = os.getenv("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
    embedding_cache_dir: Path = _path("EMBEDDING_CACHE_DIR", "models/cache")
    transcript_base_url: str = os.getenv("TRANSCRIPT_BASE_URL", "").rstrip("/")
    transcript_api_key: str = os.getenv("TRANSCRIPT_API_KEY", "")
    transcript_endpoint: str = os.getenv("TRANSCRIPT_ENDPOINT", "/transcripts")
    fx_rates_csv: Path = _path("FX_RATES_CSV", "data/fx_annual_rates.csv")


settings = Settings()

