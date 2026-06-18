from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(slots=True)
class WebsiteConfig:
    base_url: str
    seed_urls: list[str]


@dataclass(slots=True)
class WechatConfig:
    account_names: list[str]


@dataclass(slots=True)
class SourceConfig:
    id: str
    yard: str
    official_name: str
    aliases: list[str]
    website: WebsiteConfig
    wechat: WechatConfig
    group_source: bool = False


@dataclass(slots=True)
class AppConfig:
    timezone: str = "Asia/Shanghai"
    lookback_days: int = 365
    request_timeout_seconds: int = 25
    request_delay_seconds: float = 1.0
    max_list_pages_per_source: int = 8
    max_articles_per_run: int = 300
    user_agent: str = "Shipwatch/0.1"
    relevance_keywords: list[str] = field(default_factory=list)
    excluded_keywords: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Settings:
    app: AppConfig
    sources: list[SourceConfig]
    db_path: Path
    output_dir: Path
    openai_api_key: str | None
    openai_base_url: str
    openai_model: str
    openai_api_mode: str

    @property
    def yards(self) -> list[SourceConfig]:
        return [source for source in self.sources if not source.group_source]

    def source_by_id(self, source_id: str) -> SourceConfig:
        return next(source for source in self.sources if source.id == source_id)


def load_dotenv(path: str | Path = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def load_settings(path: str | Path | None = None) -> Settings:
    load_dotenv()
    config_path = Path(path or os.getenv("SHIPWATCH_CONFIG", "config.yaml"))
    raw: dict[str, Any] = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    app = AppConfig(**raw.get("app", {}))
    sources = []
    for item in raw["sources"]:
        sources.append(
            SourceConfig(
                id=item["id"],
                yard=item["yard"],
                official_name=item["official_name"],
                aliases=item.get("aliases", []),
                website=WebsiteConfig(**item["website"]),
                wechat=WechatConfig(**item["wechat"]),
                group_source=bool(item.get("group_source", False)),
            )
        )
    return Settings(
        app=app,
        sources=sources,
        db_path=Path(os.getenv("SHIPWATCH_DB", "data/shipwatch.db")),
        output_dir=Path(os.getenv("SHIPWATCH_OUTPUT_DIR", "outputs")),
        openai_api_key=os.getenv("OPENAI_API_KEY") or None,
        openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-5-mini"),
        openai_api_mode=os.getenv("OPENAI_API_MODE", "responses"),
    )
