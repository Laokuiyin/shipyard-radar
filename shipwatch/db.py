from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator

from shipwatch.domain import Article, ArticleCandidate, Extraction
from shipwatch.text import iso_date


def iso_datetime(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


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
    published_at_ts TEXT,
    fetched_at TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    content_hash TEXT,
    fetch_status TEXT NOT NULL,
    fetch_error TEXT,
    extraction_status TEXT NOT NULL DEFAULT 'pending',
    extraction_json TEXT,
    relevant INTEGER,
    discovered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    body_fetch_attempts INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
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

CREATE TABLE IF NOT EXISTS api_usage (
    id INTEGER PRIMARY KEY,
    called_at TEXT NOT NULL,
    provider TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    source_id TEXT,
    account_name TEXT,
    article_url TEXT,
    request_meta TEXT,
    success INTEGER NOT NULL,
    status_code INTEGER,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_api_usage_called_at
ON api_usage(called_at);

CREATE INDEX IF NOT EXISTS idx_api_usage_endpoint
ON api_usage(endpoint, source_id);
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
            article_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(articles)")
            }
            migrations = {
                "discovered_at": "ALTER TABLE articles ADD COLUMN discovered_at TEXT",
                "first_seen_at": "ALTER TABLE articles ADD COLUMN first_seen_at TEXT",
                "last_seen_at": "ALTER TABLE articles ADD COLUMN last_seen_at TEXT",
                "published_at_ts": "ALTER TABLE articles ADD COLUMN published_at_ts TEXT",
                "body_fetch_attempts": (
                    "ALTER TABLE articles ADD COLUMN body_fetch_attempts INTEGER NOT NULL DEFAULT 0"
                ),
                "next_retry_at": "ALTER TABLE articles ADD COLUMN next_retry_at TEXT",
            }
            for column, sql in migrations.items():
                if column not in article_columns:
                    conn.execute(sql)
            api_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(api_usage)")
            }
            if "request_meta" not in api_columns:
                conn.execute("ALTER TABLE api_usage ADD COLUMN request_meta TEXT")
            now = datetime.now().isoformat()
            conn.execute("UPDATE articles SET discovered_at=COALESCE(discovered_at, created_at, ?)", (now,))
            conn.execute("UPDATE articles SET first_seen_at=COALESCE(first_seen_at, created_at, ?)", (now,))
            conn.execute("UPDATE articles SET last_seen_at=COALESCE(last_seen_at, updated_at, ?)", (now,))

    def article_exists(self, url: str) -> bool:
        return bool(self.scalar("SELECT 1 FROM articles WHERE url=? LIMIT 1", (url,)))

    def latest_source_cursor(self, source_id: str, channel: str) -> dict:
        rows = self.query(
            """
            SELECT published_at, published_at_ts, url
            FROM articles
            WHERE source_id=? AND channel=?
            ORDER BY COALESCE(published_at_ts, published_at, first_seen_at, fetched_at) DESC, id DESC
            LIMIT 1
            """,
            (source_id, channel),
        )
        if not rows:
            return {}
        return {
            "last_seen_published_at": rows[0]["published_at"],
            "last_seen_published_at_ts": rows[0]["published_at_ts"],
            "last_seen_url": rows[0]["url"],
        }

    def upsert_discovered_article(self, candidate: ArticleCandidate) -> int:
        now = datetime.now().isoformat()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO articles (
                    source_id, yard_hint, channel, account_name, title, url,
                    published_at, published_at_ts, fetched_at, content, fetch_status,
                    discovered_at, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '', 'discovered', ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    title=COALESCE(NULLIF(excluded.title, ''), articles.title),
                    published_at=COALESCE(excluded.published_at, articles.published_at),
                    published_at_ts=COALESCE(excluded.published_at_ts, articles.published_at_ts),
                    account_name=COALESCE(excluded.account_name, articles.account_name),
                    last_seen_at=excluded.last_seen_at,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    candidate.source_id,
                    candidate.yard_hint,
                    candidate.channel,
                    candidate.account_name,
                    candidate.title,
                    candidate.url,
                    iso_date(candidate.published_at),
                    iso_datetime(candidate.published_at_ts),
                    now,
                    now,
                    now,
                    now,
                ),
            )
            return int(conn.execute("SELECT id FROM articles WHERE url=?", (candidate.url,)).fetchone()[0])

    def upsert_article(self, article: Article) -> int:
        retry_after = self._next_retry_at(article.fetch_status)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO articles (
                    source_id, yard_hint, channel, account_name, title, url,
                    published_at, published_at_ts, fetched_at, content, content_hash, fetch_status,
                    fetch_error, body_fetch_attempts, next_retry_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(url) DO UPDATE SET
                    title=excluded.title,
                    published_at=COALESCE(excluded.published_at, articles.published_at),
                    published_at_ts=COALESCE(excluded.published_at_ts, articles.published_at_ts),
                    fetched_at=excluded.fetched_at,
                    content=CASE WHEN excluded.content != '' THEN excluded.content ELSE articles.content END,
                    content_hash=COALESCE(excluded.content_hash, articles.content_hash),
                    fetch_status=excluded.fetch_status,
                    fetch_error=excluded.fetch_error,
                    body_fetch_attempts=articles.body_fetch_attempts + 1,
                    next_retry_at=?,
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
                    iso_datetime(article.published_at_ts),
                    article.fetched_at.isoformat(),
                    article.content,
                    article.content_hash,
                    article.fetch_status,
                    article.fetch_error,
                    retry_after,
                    retry_after,
                ),
            )
            row = conn.execute("SELECT id, body_fetch_attempts FROM articles WHERE url=?", (article.url,)).fetchone()
            article_id = int(row["id"])
            if article.fetch_status == "ok":
                conn.execute("UPDATE articles SET next_retry_at=NULL WHERE id=?", (article_id,))
            elif retry_after is None:
                attempts = int(row["body_fetch_attempts"] or 1)
                conn.execute(
                    "UPDATE articles SET next_retry_at=? WHERE id=?",
                    (self._next_retry_at(article.fetch_status, attempts), article_id),
                )
            return article_id

    def pending_body_fetch_articles(
        self,
        limit: int | None = None,
        source_ids: list[str] | None = None,
    ) -> list[sqlite3.Row]:
        params: list[object] = []
        source_filter = ""
        if source_ids:
            placeholders = ",".join("?" for _ in source_ids)
            source_filter = f" AND source_id IN ({placeholders})"
            params.extend(source_ids)
        sql = """
            SELECT * FROM articles
            WHERE fetch_status IN ('discovered', 'partial', 'blocked', 'error')
              AND channel='微信公众号'
              AND (next_retry_at IS NULL OR next_retry_at <= CURRENT_TIMESTAMP)
              {source_filter}
            ORDER BY
              CASE fetch_status WHEN 'discovered' THEN 0 ELSE 1 END,
              COALESCE(published_at_ts, published_at, first_seen_at, fetched_at)
        """.format(source_filter=source_filter)
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return self.query(sql, tuple(params))

    def pending_articles(self, retry_errors: bool = False) -> list[sqlite3.Row]:
        condition = "fetch_status IN ('ok', 'partial')" if retry_errors else "fetch_status='ok'"
        with self.connect() as conn:
            return list(
                conn.execute(
                    f"""
                    SELECT * FROM articles
                    WHERE extraction_status IN ('pending', 'error') AND {condition}
                    ORDER BY COALESCE(published_at_ts, published_at, fetched_at)
                    """
                )
            )

    def article_by_id(self, article_id: int) -> sqlite3.Row | None:
        rows = self.query("SELECT * FROM articles WHERE id=?", (article_id,))
        return rows[0] if rows else None

    def start_project_articles(self, limit: int | None = None) -> list[sqlite3.Row]:
        """Wechat source articles for records previously classified as starts."""
        sql = """
            SELECT DISTINCT a.*
            FROM articles a
            JOIN project_sources ps ON ps.article_id=a.id
            JOIN projects p ON p.id=ps.project_id
            WHERE p.current_progress='开工' AND a.channel='微信公众号'
            ORDER BY COALESCE(a.published_at_ts, a.published_at, a.fetched_at), a.id
        """
        if limit:
            sql += " LIMIT ?"
            return self.query(sql, (limit,))
        return self.query(sql)

    def mark_start_projects_irrelevant(self, article_id: int, reason: str | None) -> None:
        """Hide stale start candidates when their source fails the stricter re-check."""
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE projects
                SET review_status='无关', review_reason=COALESCE(?, review_reason),
                    last_changed_at=CURRENT_TIMESTAMP
                WHERE current_progress='开工' AND id IN (
                    SELECT project_id FROM project_sources WHERE article_id=?
                )
                """,
                (reason, article_id),
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
        self,
        source_key: str,
        error: str | None = None,
        result_count: int | None = None,
        cursor: dict | None = None,
    ) -> None:
        now = datetime.now().isoformat()
        cursor_json = json.dumps(cursor, ensure_ascii=False) if cursor is not None else None
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO crawl_state(
                  source_key, last_attempt_at, last_success_at, last_error, result_count, cursor
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_key) DO UPDATE SET
                  last_attempt_at=excluded.last_attempt_at,
                  last_success_at=excluded.last_success_at,
                  last_error=excluded.last_error,
                  result_count=excluded.result_count,
                  cursor=COALESCE(excluded.cursor, crawl_state.cursor)
                """,
                (source_key, now, None if error else now, error, result_count, cursor_json),
            )

    def crawl_cursor(self, source_key: str) -> dict:
        raw = self.scalar("SELECT cursor FROM crawl_state WHERE source_key=?", (source_key,))
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def record_api_call(
        self,
        provider: str,
        endpoint: str,
        source_id: str | None = None,
        account_name: str | None = None,
        article_url: str | None = None,
        request_meta: str | dict | None = None,
        success: bool = False,
        status_code: int | None = None,
        error: str | None = None,
    ) -> None:
        if isinstance(request_meta, dict):
            meta_value = json.dumps(request_meta, ensure_ascii=False)
        else:
            meta_value = request_meta
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO api_usage(
                  called_at, provider, endpoint, source_id, account_name,
                  article_url, request_meta, success, status_code, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now().isoformat(),
                    provider,
                    endpoint,
                    source_id,
                    account_name,
                    article_url,
                    meta_value,
                    int(success),
                    status_code,
                    error[:1000] if error else None,
                ),
            )

    def api_usage_summary(self, days: int = 7) -> list[sqlite3.Row]:
        return self.query(
            """
            SELECT substr(called_at, 1, 10) AS day, endpoint, source_id, account_name,
                   request_meta,
                   COUNT(*) AS calls,
                   SUM(CASE WHEN success THEN 1 ELSE 0 END) AS success_count,
                   SUM(CASE WHEN NOT success THEN 1 ELSE 0 END) AS failed_count
            FROM api_usage
            WHERE called_at >= datetime('now', ?)
            GROUP BY day, endpoint, source_id, account_name, request_meta
            ORDER BY day DESC, endpoint, source_id, account_name, request_meta
            """,
            (f"-{days} days",),
        )

    def api_usage_totals(self) -> dict:
        rows = self.query(
            """
            SELECT endpoint,
                   SUM(CASE WHEN substr(called_at, 1, 10)=date('now') THEN 1 ELSE 0 END) AS today,
                   SUM(CASE WHEN called_at >= datetime('now', '-7 days') THEN 1 ELSE 0 END) AS week,
                   COUNT(*) AS total
            FROM api_usage
            GROUP BY endpoint
            """
        )
        return {row["endpoint"]: dict(row) for row in rows}

    @staticmethod
    def _next_retry_at(status: str, attempts: int = 1) -> str | None:
        if status in {"ok", "discovered"}:
            return None
        days = (1, 3, 7, 14)[min(max(attempts - 1, 0), 3)]
        return (datetime.now() + timedelta(days=days)).isoformat()

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(conn.execute(sql, params))

    def scalar(self, sql: str, params: tuple = ()):
        with self.connect() as conn:
            row = conn.execute(sql, params).fetchone()
            return row[0] if row else None

    def project_rows(
        self,
        yard: str | None = None,
        progress: str | None = None,
        review_status: str | None = None,
        query: str | None = None,
    ) -> list[sqlite3.Row]:
        # The product is a new-start register. Historical full-lifecycle rows may
        # remain in the database for traceability, but must not appear in it.
        conditions = ["p.current_progress='开工'"]
        params: list[object] = []
        if yard:
            conditions.append("p.yard=?")
            params.append(yard)
        if progress:
            conditions.append("COALESCE(p.current_progress, '')=?")
            params.append("" if progress == "__empty__" else progress)
        if review_status:
            conditions.append("p.review_status=?")
            params.append(review_status)
        if query:
            conditions.append(
                """
                (COALESCE(p.owner_project, '') LIKE ? OR COALESCE(p.ship_type, '') LIKE ?
                 OR COALESCE(p.series_identifier, '') LIKE ? OR COALESCE(a.title, '') LIKE ?)
                """
            )
            params.extend([f"%{query}%"] * 4)
        return self.query(
            f"""
            SELECT p.*,
              a.title AS source_title, a.url AS source_url, a.published_at,
              (SELECT GROUP_CONCAT(label || COALESCE('：' || event_date, ''), '；')
               FROM milestones m WHERE m.project_id=p.id) AS milestones_text
            FROM projects p
            JOIN project_sources ps ON ps.project_id=p.id
            JOIN articles a ON a.id=ps.article_id AND a.channel='微信公众号'
            WHERE ps.article_id=(
              SELECT MAX(ps2.article_id)
              FROM project_sources ps2
              JOIN articles a2 ON a2.id=ps2.article_id
              WHERE ps2.project_id=p.id AND a2.channel='微信公众号'
            ) AND {" AND ".join(conditions)}
            ORDER BY
              CASE p.current_progress
                WHEN '开工' THEN 1 WHEN '铺龙骨' THEN 2 WHEN '下水/出坞' THEN 3
                WHEN '试航' THEN 4 WHEN '交付/完工' THEN 5 ELSE 6
              END,
              COALESCE(p.start_date, p.last_seen_at) DESC
            """,
            tuple(params),
        )
