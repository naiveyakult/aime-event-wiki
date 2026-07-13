from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://event_wiki:event_wiki@localhost:5432/event_wiki"
    langgraph_database_url: str = "postgresql://event_wiki:event_wiki@localhost:5432/event_wiki"
    openai_base_url: str = "https://api.deepseek.com"
    openai_api_key: str = ""
    openai_model: str = "deepseek-v4-pro"
    llm_timeout_seconds: float = Field(default=120, ge=10, le=600)
    llm_max_retries: int = Field(default=5, ge=0, le=10)
    review_token: str = ""
    data_root: Path = Path("../recent_one_year_data")
    local_output_dir: Path = Path(".local")
    max_candidate_documents: int = Field(default=12, ge=1, le=50)
    max_body_chars: int = Field(default=6_000, ge=500)


settings = Settings()
