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
            channel="微信公众号",
            title="测试",
            url=url,
            content="测试正文",
            published_at=date(2026, 1, 1),
            fetched_at=datetime.now(),
            content_hash="x",
        )
    )


def website_article(db, url):
    from datetime import datetime
    from shipwatch.domain import Article

    return db.upsert_article(
        Article(
            source_id="hudong_zhonghua",
            yard_hint="沪东中华",
            channel="官网",
            title="历史官网",
            url=url,
            content="历史官网正文",
            published_at=date(2026, 1, 1),
            fetched_at=datetime.now(),
            content_hash="website",
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


def test_merge_near_duplicate_when_series_missing(tmp_path):
    db = Database(tmp_path / "test.db")
    db.init()
    merger = ProjectMerger(db)
    official = Extraction(
        relevant=True,
        yard="沪东中华",
        owner_project="太平船务",
        ship_type="9000TEU集装箱船",
        ship_count=2,
        series_identifier="H1962A",
        current_progress="下水/出坞",
        milestones=[Milestone("launch", "入坞", date(2026, 6, 24), evidence="H1962A入坞")],
        confidence=0.95,
    )
    repost = Extraction(
        relevant=True,
        yard="沪东中华",
        owner_project="太平船务",
        ship_type="9000TEU集装箱船",
        current_progress="开工",
        milestones=[Milestone("start", "开工", date(2026, 6, 28), evidence="1船开工")],
        confidence=0.72,
    )

    first_project = merger.merge(article(db, "https://example.com/official"), official)
    second_project = merger.merge(article(db, "https://example.com/repost"), repost)

    assert second_project == first_project
    assert db.scalar("SELECT COUNT(*) FROM projects") == 1
    assert db.scalar("SELECT COUNT(*) FROM project_sources") == 2
    assert db.scalar("SELECT series_identifier FROM projects") == "H1962A"


def test_merge_marks_possible_duplicate_when_multiple_candidates(tmp_path):
    db = Database(tmp_path / "test.db")
    db.init()
    merger = ProjectMerger(db)
    base = dict(
        relevant=True,
        yard="沪东中华",
        owner_project="太平船务",
        ship_type="9000TEU集装箱船",
        current_progress="下水/出坞",
        confidence=0.9,
    )
    merger.merge(article(db, "https://example.com/h1"), Extraction(series_identifier="H1962A", **base))
    merger.merge(article(db, "https://example.com/h2"), Extraction(series_identifier="H1963A", **base))

    duplicate_project = merger.merge(
        article(db, "https://example.com/duplicate"),
        Extraction(current_progress="开工", **{key: value for key, value in base.items() if key != "current_progress"}),
    )

    assert db.scalar("SELECT COUNT(*) FROM projects") == 3
    row = db.query("SELECT review_status, review_reason FROM projects WHERE id=?", (duplicate_project,))[0]
    assert row["review_status"] == "可能重复"
    assert "可能重复：候选项目ID" in row["review_reason"]


def test_near_duplicate_ignores_website_only_projects(tmp_path):
    db = Database(tmp_path / "test.db")
    db.init()
    merger = ProjectMerger(db)
    base = dict(
        relevant=True,
        yard="沪东中华",
        owner_project="太平船务",
        ship_type="9000TEU集装箱船",
        current_progress="开工",
        confidence=0.9,
    )
    website_project = merger.merge(
        website_article(db, "https://example.com/website-history"),
        Extraction(series_identifier="H1962A", **base),
    )

    wechat_project = merger.merge(
        article(db, "https://example.com/wechat-current"),
        Extraction(**base),
    )

    assert wechat_project != website_project
    assert db.scalar("SELECT COUNT(*) FROM projects") == 2


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


def test_merge_accepts_llm_irrelevant_review_status(tmp_path):
    db = Database(tmp_path / "test.db")
    db.init()
    merger = ProjectMerger(db)
    extraction = Extraction(
        relevant=True,
        yard="武昌船舶重工",
        owner_project="党建活动",
        ship_type="宣传报道",
        current_progress="交付/完工",
        confidence=0.95,
        review_status="无关",
        review_reason="DeepSeek 判定为非新船/海工项目",
    )

    merger.merge(article(db, "https://example.com/irrelevant"), extraction)

    assert db.scalar("SELECT review_status FROM projects") == "无关"
