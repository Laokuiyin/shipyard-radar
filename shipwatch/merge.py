from __future__ import annotations

import json
from datetime import date, datetime

from shipwatch.db import Database
from shipwatch.domain import Extraction
from shipwatch.text import compact_name, iso_date


PROGRESS_RANK = {
    "签约/立项": 10,
    "开工": 20,
    "铺龙骨": 30,
    "下水/出坞": 40,
    "试航": 50,
    "交付/完工": 60,
}

REVIEW_STATUSES = {"已确认", "待复核", "可能重复", "无关"}
NEAR_DUPLICATE_DAYS = 14


def review_status_for(extraction: Extraction) -> str:
    if extraction.review_status in REVIEW_STATUSES:
        return extraction.review_status
    return "已确认" if extraction.confidence >= 0.82 and not extraction.review_reason else "待复核"


def project_key(extraction: Extraction, article_id: int) -> str:
    parts = [
        compact_name(extraction.yard),
        compact_name(extraction.owner_project),
        compact_name(extraction.ship_type),
        compact_name(extraction.series_identifier),
    ]
    stable = "|".join(parts)
    if sum(bool(part) for part in parts[1:]) < 2:
        return f"{parts[0]}|article:{article_id}"
    return stable


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _compatible_name(left: str | None, right: str | None) -> bool:
    left_key = compact_name(left)
    right_key = compact_name(right)
    if not left_key or not right_key:
        return False
    return left_key == right_key or left_key in right_key or right_key in left_key


def _compatible_ship_type(left: str | None, right: str | None) -> bool:
    left_key = compact_name(left)
    right_key = compact_name(right)
    if not left_key or not right_key:
        return False
    return left_key == right_key or left_key in right_key or right_key in left_key


def _series_can_match(left: str | None, right: str | None) -> bool:
    left_key = compact_name(left)
    right_key = compact_name(right)
    return not left_key or not right_key or left_key == right_key


def _dates_near(left: date | None, right: date | None) -> bool:
    if not left or not right:
        return True
    return abs((left - right).days) <= NEAR_DUPLICATE_DAYS


def records_are_near_duplicates(left, right) -> bool:
    """Apply the established project de-duplication rule to two project records."""
    return (
        left["yard"] == right["yard"]
        and _compatible_name(left["owner_project"], right["owner_project"])
        and _compatible_ship_type(left["ship_type"], right["ship_type"])
        and _series_can_match(left["series_identifier"], right["series_identifier"])
        and _dates_near(
            _parse_date(left["latest_published_at"]),
            _parse_date(right["latest_published_at"]),
        )
    )


class ProjectMerger:
    def __init__(self, db: Database):
        self.db = db

    def _near_duplicate_candidates(self, conn, extraction: Extraction, article_date: date | None):
        rows = conn.execute(
            """
            SELECT p.*, MAX(a.published_at) AS latest_published_at
            FROM projects p
            LEFT JOIN project_sources ps ON ps.project_id=p.id
            LEFT JOIN articles a ON a.id=ps.article_id
            WHERE p.yard=? AND EXISTS (
              SELECT 1
              FROM project_sources wps
              JOIN articles wa ON wa.id=wps.article_id
              WHERE wps.project_id=p.id AND wa.channel='微信公众号'
            )
            GROUP BY p.id
            """,
            (extraction.yard,),
        ).fetchall()
        candidates = []
        for row in rows:
            incoming = {
                "yard": extraction.yard,
                "owner_project": extraction.owner_project,
                "ship_type": extraction.ship_type,
                "series_identifier": extraction.series_identifier,
                "latest_published_at": iso_date(article_date),
            }
            if records_are_near_duplicates(incoming, row):
                candidates.append(row)
        return candidates

    def merge(self, article_id: int, extraction: Extraction) -> int | None:
        if not extraction.relevant or not extraction.yard:
            return None
        key = project_key(extraction, article_id)
        now = datetime.now().isoformat()
        with self.db.connect() as conn:
            existing = conn.execute("SELECT * FROM projects WHERE project_key=?", (key,)).fetchone()
            article_row = conn.execute("SELECT published_at FROM articles WHERE id=?", (article_id,)).fetchone()
            article_date = _parse_date(article_row["published_at"] if article_row else None)
            review_status = review_status_for(extraction)
            if not existing:
                near_duplicates = self._near_duplicate_candidates(conn, extraction, article_date)
                if len(near_duplicates) == 1:
                    existing = near_duplicates[0]
                    key = existing["project_key"]
                elif len(near_duplicates) > 1:
                    review_status = "可能重复"
                    duplicate_ids = "、".join(str(row["id"]) for row in near_duplicates)
                    extraction.review_reason = "；".join(
                        item
                        for item in (
                            extraction.review_reason,
                            f"可能重复：候选项目ID {duplicate_ids}",
                        )
                        if item
                    )
            if existing:
                conflicts = []
                incoming_ship_count = extraction.ship_count
                incoming_ship_type = extraction.ship_type
                if (
                    extraction.ship_count is not None
                    and existing["ship_count"] is not None
                    and extraction.ship_count != existing["ship_count"]
                ):
                    conflicts.append(
                        f"船数冲突：原记录{existing['ship_count']}艘，新来源{extraction.ship_count}艘"
                    )
                    incoming_ship_count = None
                if (
                    extraction.ship_type
                    and existing["ship_type"]
                    and compact_name(extraction.ship_type) != compact_name(existing["ship_type"])
                ):
                    conflicts.append(
                        f"船型冲突：原记录“{existing['ship_type']}”，新来源“{extraction.ship_type}”"
                    )
                    incoming_ship_type = None
                if conflicts:
                    review_status = "待复核"
                    extraction.review_reason = "；".join(
                        item
                        for item in (extraction.review_reason, *conflicts)
                        if item
                    )
                current = existing["current_progress"]
                incoming = extraction.current_progress
                progress = (
                    incoming
                    if PROGRESS_RANK.get(incoming, 0) >= PROGRESS_RANK.get(current, 0)
                    else current
                )
                changed = any(
                    (
                        extraction.owner_project and extraction.owner_project != existing["owner_project"],
                        extraction.ship_type and extraction.ship_type != existing["ship_type"],
                        extraction.ship_count and extraction.ship_count != existing["ship_count"],
                        progress != current,
                        extraction.start_date and iso_date(extraction.start_date) != existing["start_date"],
                        extraction.completion_date
                        and iso_date(extraction.completion_date) != existing["completion_date"],
                    )
                )
                conn.execute(
                    """
                    UPDATE projects SET
                      owner_project=COALESCE(?, owner_project),
                      ship_type=COALESCE(?, ship_type),
                      ship_count=COALESCE(?, ship_count),
                      series_identifier=COALESCE(?, series_identifier),
                      current_progress=?,
                      start_date=COALESCE(?, start_date),
                      completion_date=COALESCE(?, completion_date),
                      confidence=MAX(confidence, ?),
                      review_status=CASE
                        WHEN ?='可能重复' THEN '可能重复'
                        WHEN ?='待复核' THEN '待复核'
                        WHEN ?='无关' THEN '无关'
                        WHEN review_status NOT IN ('待复核', '可能重复') THEN ?
                        ELSE review_status
                      END,
                      review_reason=COALESCE(?, review_reason),
                      last_seen_at=?,
                      last_changed_at=CASE WHEN ? THEN ? ELSE last_changed_at END
                    WHERE id=?
                    """,
                    (
                        extraction.owner_project,
                        incoming_ship_type,
                        incoming_ship_count,
                        extraction.series_identifier,
                        progress,
                        iso_date(extraction.start_date),
                        iso_date(extraction.completion_date),
                        extraction.confidence,
                        review_status,
                        review_status,
                        review_status,
                        review_status,
                        extraction.review_reason,
                        now,
                        int(changed),
                        now,
                        existing["id"],
                    ),
                )
                project_id = int(existing["id"])
            else:
                cursor = conn.execute(
                    """
                    INSERT INTO projects (
                      project_key, yard, owner_project, ship_type, ship_count, series_identifier,
                      current_progress, start_date, completion_date, confidence, review_status,
                      review_reason, first_seen_at, last_seen_at, last_changed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        key,
                        extraction.yard,
                        extraction.owner_project,
                        extraction.ship_type,
                        extraction.ship_count,
                        extraction.series_identifier,
                        extraction.current_progress,
                        iso_date(extraction.start_date),
                        iso_date(extraction.completion_date),
                        extraction.confidence,
                        review_status,
                        extraction.review_reason,
                        now,
                        now,
                        now,
                    ),
                )
                project_id = int(cursor.lastrowid)

            # Store project_key in article's extraction_json for source lookup
            row = conn.execute(
                "SELECT extraction_json FROM articles WHERE id=?", (article_id,)
            ).fetchone()
            if row and row[0]:
                try:
                    payload = json.loads(row[0])
                    payload["project_key"] = key
                    conn.execute(
                        "UPDATE articles SET extraction_json=? WHERE id=?",
                        (json.dumps(payload, ensure_ascii=False), article_id),
                    )
                except (json.JSONDecodeError, TypeError):
                    pass

            conn.execute(
                """
                INSERT INTO project_sources(project_id, article_id, confidence)
                VALUES (?, ?, ?) ON CONFLICT(project_id, article_id) DO UPDATE SET
                confidence=excluded.confidence
                """,
                (project_id, article_id, extraction.confidence),
            )
            for milestone in extraction.milestones:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO milestones(
                      project_id, article_id, kind, label, event_date, is_expected, evidence
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        project_id,
                        article_id,
                        milestone.kind,
                        milestone.label,
                        iso_date(milestone.event_date),
                        int(milestone.is_expected),
                        milestone.evidence,
                    ),
                )
            return project_id
