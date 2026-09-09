from functools import lru_cache
from dataclasses import dataclass
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict



@dataclass(frozen=True, slots=True)
class StorageLimits:
    asset_root_quota_bytes: int
    cache_root_quota_bytes: int
    temporary_quota_bytes: int
    shared_headroom_bytes: int
    shared_headroom_fraction: float

    def shared_headroom(self, filesystem_bytes: int) -> int:
        if filesystem_bytes < 0:
            raise ValueError("filesystem byte count cannot be negative")
        return max(self.shared_headroom_bytes, int(filesystem_bytes * self.shared_headroom_fraction))

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TITLES_", env_file=".env", extra="ignore")

    app_name: str = "Titles DAM"
    database_url: str = "sqlite:///./var/titles-dam.sqlite3"
    wandb_ingress_enabled: bool = False
    wandb_ingress_database_url: str | None = None
    wandb_ingress_base_url: str | None = None
    wandb_upload_root: Path = Path("var/wandb-uploads")
    wandb_stale_after_seconds: int = 900
    wandb_ingress_rate_limit_per_minute: int = 6000
    wandb_graphql_max_bytes: int = 1024 * 1024
    wandb_filestream_max_bytes: int = 8 * 1024 * 1024
    wandb_image_max_bytes: int = 64 * 1024 * 1024
    wandb_config_max_bytes: int = 4 * 1024 * 1024
    asset_root: Path = Path("var/assets")
    cache_root: Path = Path("var/cache")
    export_root: Path = Path("var/exports")
    config_root: Path = Path("var/config")
    asset_root_quota_bytes: int = 100 * 1024**3
    cache_root_quota_bytes: int = 10 * 1024**3
    temporary_quota_bytes: int = 10 * 1024**3
    shared_headroom_bytes: int = 10 * 1024**3
    shared_headroom_fraction: float = 0.10
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    default_workspace_name: str = "Local Workspace"
    default_profile_name: str = "Operator"

    @field_validator("cors_origins", mode="before")
    @classmethod
    def split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator(
        "asset_root_quota_bytes",
        "cache_root_quota_bytes",
        "temporary_quota_bytes",
        "shared_headroom_bytes",
    )
    @classmethod
    def positive_storage_limit(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("storage limits must be positive")
        return value

    @field_validator(
        "wandb_stale_after_seconds",
        "wandb_ingress_rate_limit_per_minute",
        "wandb_graphql_max_bytes",
        "wandb_filestream_max_bytes",
        "wandb_image_max_bytes",
        "wandb_config_max_bytes",
    )
    @classmethod
    def positive_wandb_limit(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("W&B ingress limits must be positive")
        return value

    @field_validator("shared_headroom_fraction")
    @classmethod
    def valid_headroom_fraction(cls, value: float) -> float:
        if not 0 < value <= 1:
            raise ValueError("shared headroom fraction must be in (0, 1]")
        return value

    def ensure_runtime_dirs(self) -> None:
        for root in (self.asset_root, self.cache_root, self.export_root, self.config_root, self.wandb_upload_root):
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            root.chmod(0o700)
        if self.database_url.startswith("sqlite:///./"):
            Path(self.database_url.removeprefix("sqlite:///./")).parent.mkdir(parents=True, exist_ok=True)

    def storage_limits(self) -> StorageLimits:
        """Return numeric capacity policy without exposing configured root paths."""
        return StorageLimits(
            asset_root_quota_bytes=self.asset_root_quota_bytes,
            cache_root_quota_bytes=self.cache_root_quota_bytes,
            temporary_quota_bytes=self.temporary_quota_bytes,
            shared_headroom_bytes=self.shared_headroom_bytes,
            shared_headroom_fraction=self.shared_headroom_fraction,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
