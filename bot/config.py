import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    token: str
    base_url: str
    database_url: str
    tz: str

    @staticmethod
    def from_env() -> "Config":
        return Config(
            token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            base_url=os.getenv("BASE_URL", "https://example.com").rstrip("/"),
            database_url=os.getenv("DATABASE_URL", "postgresql://postgres:pg@localhost:5432/courses"),
            tz=os.getenv("BOT_TZ", "Europe/Moscow"),
        )
