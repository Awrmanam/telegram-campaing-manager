from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    bot_token: str = ""
    admin_ids: frozenset[int] = frozenset()
    telegram_api_id: int = 0
    telegram_api_hash: str = ""
    database_url: str = "sqlite+aiosqlite:///./data/app.db"
    timezone: str = "Asia/Tehran"
    session_directory: Path = Path("./sessions")
    media_directory: Path = Path("./media")
    log_level: str = "INFO"
    min_send_delay_seconds: float = 3
    max_send_delay_seconds: float = 8
    catch_up_missed_runs: bool = True
    alert_cooldown_seconds: int = 3600

    @field_validator("admin_ids", mode="before")
    @classmethod
    def parse_admins(cls, value: object) -> frozenset[int]:
        if isinstance(value, str):
            return frozenset(int(item.strip()) for item in value.split(",") if item.strip())
        return frozenset(value or [])

    def prepare_directories(self) -> None:
        for directory in (self.session_directory, self.media_directory):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)


@lru_cache
def get_settings() -> Settings:
    return Settings()
