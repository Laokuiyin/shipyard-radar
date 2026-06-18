from datetime import date, datetime

from fastapi.testclient import TestClient

from shipwatch.config import load_settings
from shipwatch.db import Database
from shipwatch.domain import Article, Extraction, Milestone
from shipwatch.merge import ProjectMerger
from shipwatch.web import create_app


def test_dashboard_and_health(tmp_path):
    settings = load_settings("config.yaml")
    settings.db_path = tmp_path / "web.db"
    db = Database(settings.db_path)
    db.init()
    article_id = db.upsert_article(
        Article(
            source_id="waigaoqiao",
            yard_hint="上海外高桥造船",
            channel="官网",
            title="新船开工",
            url="https://example.com/project",
            content="外高桥造船一艘集装箱船开工",
            published_at=date(2026, 6, 1),
            fetched_at=datetime.now(),
            content_hash="web",
        )
    )
    extraction = Extraction(
        relevant=True,
        yard="上海外高桥造船",
        owner_project="测试船东",
        ship_type="集装箱船",
        ship_count=1,
        current_progress="开工",
        milestones=[Milestone("start", "开工", date(2026, 6, 1), evidence="正式开工")],
        confidence=0.95,
    )
    db.mark_extracted(article_id, extraction)
    ProjectMerger(db).merge(article_id, extraction)

    client = TestClient(create_app(settings))
    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert "测试船东" in dashboard.text
    assert "新船项目主表" in dashboard.text
    assert client.get("/health").json()["projects"] == 1
    assert client.get("/source-status").status_code == 200
