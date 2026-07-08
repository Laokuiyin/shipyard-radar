from __future__ import annotations

import argparse
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from shipwatch.text import normalize_url


def find_obsolete_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    ok_urls = {
        row["url"]
        for row in conn.execute(
            """
            SELECT url FROM articles
            WHERE fetch_status='ok' AND url LIKE 'https://mp.weixin.qq.com/s/%'
            """
        )
    }
    rows = []
    for row in conn.execute(
        """
        SELECT id, title, url, fetch_status, length(content) AS content_len
        FROM articles
        WHERE url LIKE 'https://mp.weixin.qq.com/mp/wappoc_appmsgcaptcha%target_url=%'
        ORDER BY id
        """
    ):
        if normalize_url(row["url"]) in ok_urls:
            rows.append(row)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/shipwatch.db")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    db_path = Path(args.db)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = find_obsolete_rows(conn)
        print(f"obsolete captcha duplicate rows: {len(rows)}")
        for row in rows[:20]:
            print(f"{row['id']}\t{row['fetch_status']}\t{row['content_len']}\t{row['title']}")
        if len(rows) > 20:
            print(f"... {len(rows) - 20} more")
        if not args.apply or not rows:
            return

        backup_path = db_path.with_suffix(
            db_path.suffix + f".bak_obsolete_captcha_{datetime.now():%Y%m%d_%H%M%S}"
        )
        shutil.copy2(db_path, backup_path)
        ids = [int(row["id"]) for row in rows]
        placeholders = ",".join("?" for _ in ids)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"DELETE FROM articles WHERE id IN ({placeholders})", ids)
        conn.commit()
        print(f"deleted: {len(ids)}")
        print(f"backup: {backup_path}")


if __name__ == "__main__":
    main()
