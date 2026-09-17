from __future__ import annotations

from enum import Enum
from functools import lru_cache

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(str, Enum):
    local = "local"
    staging = "staging"
    production = "production"


class StorageBackend(str, Enum):
    local = "local"
    azure = "azure"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env.local", ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Runtime ────────────────────────────────────────────────────────────
    environment: Environment = Environment.local

    # ── Auth ───────────────────────────────────────────────────────────────
    auth_enabled: bool = False   # OAuth2 enforced only when True

    # JWT — backend issues its own HS256 tokens (no external IdP)
    jwt_secret: str = "change-me-in-production"   # override in .env
    jwt_algorithm: str = "HS256"
    token_ttl_seconds: int = 7200                 # 2-hour token lifetime

    # Users: "username:password:role,username2:password2"
    # Role defaults to "broker" if omitted.
    danube_users: str = "admin:danube2024:admin"

    # ── Azure OpenAI ───────────────────────────────────────────────────────
    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    azure_openai_api_version: str = "2024-08-01-preview"
    azure_openai_deployment: str = ""
    azure_openai_embedding_deployment: str = ""

    # ── LangSmith ─────────────────────────────────────────────────────────
    langsmith_api_key: str = ""
    langsmith_endpoint: str = "https://aws.api.smith.langchain.com"
    langsmith_project: str = "Danube_One_Chatbot"
    langsmith_dataset: str = "danube-phase1a-golden"

    # ── Vector store (Chroma — local persistent) ───────────────────────────────
    chroma_path: str = "./chroma_db"

    # ── Database ──────────────────────────────────────────────────────────
    postgres_url: str = "postgresql+asyncpg://danube:danube@localhost:5432/danube"

    # ── Asset metadata API (wired up later — dummy registry used for now) ──────
    asset_api_base_url: str = ""   # e.g. https://your-api.com/api

    # ── Storage ───────────────────────────────────────────────────────────
    storage_backend: StorageBackend = StorageBackend.local
    local_storage_path: str = "./local_storage"
    azure_storage_account_url: str = ""
    azure_storage_container_name: str = "danube-assets"

    # ── Signed URLs ────────────────────────────────────────────────────────
    signed_url_ttl_seconds: int = 900

    # ── Salesforce ────────────────────────────────────────────────────────
    sf_instance_url: str = "https://danubeproperties.my.salesforce.com"
    sf_client_id: str = ""
    sf_client_secret: str = ""
    sf_api_timeout_seconds: float = 15.0
    # Fraction of daily quota remaining below which a warning is logged (0.0-1.0)
    sf_quota_warn_threshold: float = 0.10
    # TTL in seconds for the in-memory project directory cache
    sf_project_cache_ttl_seconds: int = 1200  # 20 minutes

    # ── Agent ─────────────────────────────────────────────────────────────
    fast_path_threshold: float = 0.85

    @field_validator("azure_openai_endpoint", "azure_openai_api_key", mode="before")
    @classmethod
    def _not_placeholder(cls, v: str) -> str:
        if v and v.startswith("<"):
            return ""
        return v

    @model_validator(mode="after")
    def _validate_required_for_non_local(self) -> Settings:
        if self.environment != Environment.local:
            missing = []
            for field in ("azure_openai_endpoint", "azure_openai_api_key",
                          "azure_openai_deployment", "azure_openai_embedding_deployment"):
                if not getattr(self, field):
                    missing.append(field)
            if missing:
                raise ValueError(f"Required settings missing for non-local environment: {missing}")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
