from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from shipwatch.domain import Article, Extraction
from shipwatch.text import iso_date


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY,
    source_id TEXT NOT NULL,
    yard_hint TEXT NOT NULL,
    channel TEXT NOT NULL,
    account_name TEXT,
    title TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    published_at TEXT,
    fetched_at TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    content_hash TEXT,
    fetch_status TEXT NOT NULL,
    fetch_error TEXT,
    extraction_status TEXT NOT NULL DEFAULT 'pending',
    extraction_json TEXT,
    relevant INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_articles_pending
ON articles(extraction_status, fetch_status);

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    project_key TEXT NOT NULL UNIQUE,
    yard TEXT NOT NULL,
    owner_project TEXT,
    ship_type TEXT,
    ship_count INTEGER,
    series_identifier TEXT,
    current_progress TEXT,
    start_date TEXT,
    completion_date TEXT,
    confidence REAL NOT NULL DEFAULT 0,
    review_status TEXT NOT NULL DEFAULT '待复核',
    review_reason TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_changed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS milestones (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    label TEXT NOT NULL,
    event_date TEXT,
    is_expected INTEGER NOT NULL DEFAULT 0,
    evidence TEXT,
    UNIQUE(project_id, article_id, kind, event_date, label)
);

CREATE TABLE IF NOT EXISTS project_sources (
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    article_id INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    confidence REAL NOT NULL,
    PRIMARY KEY(project_id, article_id)
);

CREATE TABLE IF NOT EXISTS crawl_state (
    source_key TEXT PRIMARY KEY,
    last_attempt_at TEXT,
    last_success_at TEXT,
    last_error TEXT,
    result_count INTEGER,
    cursor TEXT
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(crawl_state)")
            }
            if "last_attempt_at" not in columns:
                conn.execute("ALTER TABLE crawl_state ADD COLUMN last_attempt_at TEXT")
            if "result_count" not in columns:
                conn.execute("ALTER TABLE crawl_state ADD COLUMN result_count INTEGER")

    def upsert_article(self, article: Article) -> int:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO articles (
                    source_id, yard_hint, channel, account_name, title, url,
                    published_at, fetched_at, content, content_hash, fetch_status, fetch_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    title=excluded.title,
                    published_at=COALESCE(excluded.published_at, articles.published_at),
                    fetched_at=excluded.fetched_at,
                    content=CASE WHEN excluded.content != '' THEN excluded.content ELSE articles.content END,
                    content_hash=COALESCE(excluded.content_hash, articles.content_hash),
                    fetch_status=excluded.fetch_status,
                    fetch_error=excluded.fetch_error,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    article.source_id,
                    article.yard_hint,
                    article.channel,
                    article.account_name,
                    article.title,
                    article.url,
                    iso_date(article.published_at),
                    article.fetched_at.isoformat(),
                    article.content,
                    article.content_hash,
                    article.fetch_status,
                    article.fetch_error,
                ),
            )
            return int(conn.execute("SELECT id FROM articles WHERE url=?", (article.url,)).fetchone()[0])

    def pending_articles(self, retry_errors: bool = False) -> list[sqlite3.Row]:
        condition = "fetch_status IN ('ok', 'partial')" if retry_errors else "fetch_status='ok'"
        with self.connect() as conn:
            return list(
                conn.execute(
                    f"""
                    SELECT * FROM articles
                    WHERE extraction_status IN ('pending', 'error') AND {condition}
                    ORDER BY COALESCE(published_at, fetched_at)
                    """
                )
            )

    def mark_extracted(self, article_id: int, extraction: Extraction) -> None:
        payload = {
            "relevant": extraction.relevant,
            "yard": extraction.yard,
            "owner_project": extraction.owner_project,
            "ship_type": extraction.ship_type,
            "ship_count": extraction.ship_count,
            "series_identifier": extraction.series_identifier,
            "current_progress": extraction.current_progress,
            "start_date": iso_date(extraction.start_date),
            "completion_date": iso_date(extraction.completion_date),
            "confidence": extraction.confidence,
            "review_reason": extraction.review_reason,
            "milestones": [
                {
                    "kind": item.kind,
                    "label": item.label,
                    "event_date": iso_date(item.event_date),
                    "is_expected": item.is_expected,
                    "evidence": item.evidence,
                }
                for item in extraction.milestones
            ],
            "raw": extraction.raw,
        }
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE articles SET extraction_status='done', extraction_json=?,
                relevant=?, updated_at=CURRENT_TIMESTAMP WHERE id=?
                """,
                (json.dumps(payload, ensure_ascii=False), int(extraction.relevant), article_id),
            )

    def mark_extraction_error(self, article_id: int, error: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE articles SET extraction_status='error', fetch_error=?,
                updated_at=CURRENT_TIMESTAMP WHERE id=?
                """,
                (error[:1000], article_id),
            )

    def set_crawl_state(
        self, source_key: str, error: str | None = None, result_count: int | None = None
    ) -> None:
        now = datetime.now().isoformat()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO crawl_state(
                  source_key, last_attempt_at, last_success_at, last_error, result_count
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source_key) DO UPDATE SET
                  last_attempt_at=excluded.last_attempt_at,
                  last_success_at=excluded.last_success_at,
                  last_error=excluded.last_error,
                  result_count=excluded.result_count
                """,
                (source_key, now, None if error else now, error, result_count),
            )

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(conn.execute(sql, params))

    def scalar(self, sql: str, params: tuple = ()):
        with self.connect() as conn:
            row = conn.execute(sql, params).fetchone()
            return row[0] if row else None
