from datetime import date

from shipwatch.db import Database
from shipwatch.domain import Extraction, Milestone
from shipwatch.merge import ProjectMerger


def article(db, url):
    from datetime import datetime
    from shipwatch.domain import Article

    return db.upsert_article(
        Article(
            source_id="hudong_zhonghua",
            yard_hint="沪东中华",
            channel="官网",
            title="测试",
            url=url,
            content="测试正文",
            published_at=date(2026, 1, 1),
            fetched_at=datetime.now(),
            content_hash="x",
        )
    )


def test_merge_progress_and_deduplicate(tmp_path):
    db = Database(tmp_path / "test.db")
    db.init()
    merger = ProjectMerger(db)
    first = Extraction(
        relevant=True,
        yard="沪东中华",
        owner_project="某航运",
        ship_type="LNG船",
        ship_count=2,
        current_progress="开工",
        milestones=[Milestone("start", "开工", date(2026, 1, 1), evidence="正式开工")],
        confidence=0.9,
    )
    second = Extraction(
        relevant=True,
        yard="沪东中华",
        owner_project="某航运",
        ship_type="LNG船",
        ship_count=2,
        current_progress="下水/出坞",
        milestones=[Milestone("launch", "下水/出坞", date(2026, 6, 1), evidence="顺利出坞")],
        confidence=0.9,
    )
    merger.merge(article(db, "https://example.com/1"), first)
    merger.merge(article(db, "https://example.com/2"), second)
    assert db.scalar("SELECT COUNT(*) FROM projects") == 1
    assert db.scalar("SELECT current_progress FROM projects") == "下水/出坞"
    assert db.scalar("SELECT COUNT(*) FROM milestones") == 2


def test_conflicting_ship_count_is_flagged_without_overwrite(tmp_path):
    db = Database(tmp_path / "test.db")
    db.init()
    merger = ProjectMerger(db)
    base = dict(
        relevant=True,
        yard="江南造船",
        owner_project="某航运",
        ship_type="集装箱船",
        current_progress="签约/立项",
        confidence=0.9,
    )
    merger.merge(article(db, "https://example.com/a"), Extraction(ship_count=4, **base))
    merger.merge(article(db, "https://example.com/b"), Extraction(ship_count=6, **base))
    assert db.scalar("SELECT ship_count FROM projects") == 4
    assert db.scalar("SELECT review_status FROM projects") == "待复核"
    assert "船数冲突" in db.scalar("SELECT review_reason FROM projects")
