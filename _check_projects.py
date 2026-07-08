from shipwatch.db import Database
from shipwatch.config import load_settings
db = Database(load_settings().db_path)
rows = db.project_rows()
if rows:
    print("Keys:", list(rows[0].keys()))
    for r in rows[:5]:
        src_url = "yes" if r["source_url"] else "no"
        src_title = "yes" if r["source_title"] else "no"
        print(f'id={r["id"]}, yard={r["yard"]}, has_url={src_url}, has_title={src_title}')
        if not r["source_url"]:
            print(f'  -> NO source_url for project {r["id"]}')
