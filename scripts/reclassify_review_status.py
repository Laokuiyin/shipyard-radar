from __future__ import annotations

import argparse
import json
from datetime import date

from shipwatch.config import load_settings
from shipwatch.db import Database
from shipwatch.extract import LLMExtractor


REVIEW_STATUSES = {"已确认", "待复核", "无关"}


def status_from_extraction(extraction) -> str:
    if extraction.review_status in REVIEW_STATUSES:
        return extraction.review_status
    if not extraction.relevant:
        return "无关"
    return "已确认" if extraction.confidence >= 0.82 and not extraction.review_reason else "待复核"


def load_rows(db: Database, status: str, limit: int | None) -> list:
    sql = """
        SELECT p.id AS project_id, p.yard, p.owner_project, p.ship_type, p.ship_count,
               p.current_progress, p.review_status, p.review_reason,
               a.id AS article_id, a.title, a.content, a.published_at, a.yard_hint
        FROM projects p
        JOIN project_sources ps ON ps.project_id=p.id
        JOIN articles a ON a.id=ps.article_id
        WHERE p.review_status=?
          AND ps.article_id=(
            SELECT MAX(ps2.article_id) FROM project_sources ps2 WHERE ps2.project_id=p.id
          )
        ORDER BY p.last_seen_at DESC, p.id DESC
    """
    params: list[object] = [status]
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return db.query(sql, tuple(params))


def apply_status(db: Database, project_id: int, article_id: int, status: str, reason: str | None, raw: dict) -> None:
    with db.connect() as conn:
        conn.execute(
            """
            UPDATE projects
            SET review_status=?, review_reason=?, last_changed_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (status, reason, project_id),
        )
        row = conn.execute("SELECT extraction_json FROM articles WHERE id=?", (article_id,)).fetchone()
        payload = {}
        if row and row["extraction_json"]:
            try:
                payload = json.loads(row["extraction_json"])
            except json.JSONDecodeError:
                payload = {}
        payload["review_status_reclassified"] = {
            "status": status,
            "reason": reason,
            "raw": raw,
        }
        conn.execute(
            "UPDATE articles SET extraction_json=? WHERE id=?",
            (json.dumps(payload, ensure_ascii=False), article_id),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="使用 DeepSeek 重新分类项目复核状态")
    parser.add_argument("--config", help="配置文件路径")
    parser.add_argument("--status", default="待复核", help="待重分类的原状态")
    parser.add_argument("--limit", type=int, default=20, help="最多处理条数；0 表示不限制")
    parser.add_argument("--apply", action="store_true", help="写回数据库；默认只打印结果")
    args = parser.parse_args()

    settings = load_settings(args.config)
    if not settings.openai_api_key:
        raise SystemExit("OPENAI_API_KEY 未配置，无法调用 DeepSeek")
    db = Database(settings.db_path)
    db.init()
    rows = load_rows(db, args.status, None if args.limit == 0 else args.limit)
    extractor = LLMExtractor(settings)
    print("project_id\told_status\tnew_status\tconfidence\treason\ttitle")
    try:
        for row in rows:
            published = date.fromisoformat(row["published_at"]) if row["published_at"] else None
            context = "\n".join(
                item
                for item in [
                    f"当前项目字段：船厂={row['yard']}；船东/项目={row['owner_project'] or '未知'}；"
                    f"船型={row['ship_type'] or '未知'}；船数={row['ship_count'] if row['ship_count'] is not None else '未知'}；"
                    f"进度={row['current_progress'] or '未知'}。",
                    row["content"] or "",
                ]
                if item
            )
            try:
                extraction = extractor.extract(
                    title=row["title"],
                    content=context,
                    yard_hint=row["yard_hint"] or row["yard"],
                    published_at=published,
                )
            except Exception as exc:
                print(
                    f"{row['project_id']}\t{row['review_status']}\tERROR\t"
                    f"0.00\t{type(exc).__name__}: {exc}\t{row['title']}"
                )
                continue
            new_status = status_from_extraction(extraction)
            reason = extraction.review_reason
            if new_status != "已确认" and not reason:
                reason = "DeepSeek 判定为" + new_status
            print(
                f"{row['project_id']}\t{row['review_status']}\t{new_status}\t"
                f"{extraction.confidence:.2f}\t{reason or ''}\t{row['title']}"
            )
            if args.apply:
                apply_status(
                    db,
                    int(row["project_id"]),
                    int(row["article_id"]),
                    new_status,
                    reason,
                    extraction.raw,
                )
    finally:
        extractor.client.close()


if __name__ == "__main__":
    main()
