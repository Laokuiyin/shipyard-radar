from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any


@dataclass(slots=True)
class ArticleCandidate:
    source_id: str
    yard_hint: str
    channel: str
    title: str
    url: str
    published_at: date | None = None
    published_at_ts: datetime | None = None
    account_name: str | None = None


@dataclass(slots=True)
class Article:
    source_id: str
    yard_hint: str
    channel: str
    title: str
    url: str
    content: str
    published_at: date | None
    fetched_at: datetime
    published_at_ts: datetime | None = None
    account_name: str | None = None
    fetch_status: str = "ok"
    fetch_error: str | None = None
    content_hash: str = ""


@dataclass(slots=True)
class Milestone:
    kind: str
    label: str
    event_date: date | None = None
    is_expected: bool = False
    evidence: str = ""


@dataclass(slots=True)
class Extraction:
    relevant: bool
    yard: str | None = None
    owner_project: str | None = None
    ship_type: str | None = None
    ship_count: int | None = None
    series_identifier: str | None = None
    current_progress: str | None = None
    start_date: date | None = None
    completion_date: date | None = None
    milestones: list[Milestone] = field(default_factory=list)
    confidence: float = 0.0
    review_reason: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
