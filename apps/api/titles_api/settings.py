from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TITLES_", env_file=".env", extra="ignore")

    app_name: str = "Titles DAM"
    database_url: str = "sqlite:///./var/titles-dam.sqlite3"
    asset_root: Path = Path("var/assets")
    cache_root: Path = Path("var/cache")
    export_root: Path = Path("var/exports")
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    default_workspace_name: str = "Local Workspace"
    default_profile_name: str = "Operator"

    @field_validator("cors_origins", mode="before")
    @classmethod
    def split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    def ensure_runtime_dirs(self) -> None:
        self.asset_root.mkdir(parents=True, exist_ok=True)
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.export_root.mkdir(parents=True, exist_ok=True)
        if self.database_url.startswith("sqlite:///./"):
            Path(self.database_url.removeprefix("sqlite:///./")).parent.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
