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
            channel="微信公众号",
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
    assert "项目总数" in dashboard.text
    assert "最近采集" in dashboard.text
    assert "最近项目更新" in dashboard.text
    assert "待处理文章" in dashboard.text
    assert "已确认" in dashboard.text
    assert 'class="mobile-list"' in dashboard.text
    assert 'class="mobile-card"' in dashboard.text
    assert 'href="/source-config"' not in dashboard.text
    assert client.get("/health").json()["projects"] == 1
    assert client.get("/source-status").status_code == 200
    assert client.get("/source-config").status_code == 200


def test_dashboard_source_errors_do_not_change_project_metrics(tmp_path):
    settings = load_settings("config.yaml")
    settings.db_path = tmp_path / "web.db"
    db = Database(settings.db_path)
    db.init()
    db.set_crawl_state("jiangnan:website", "legacy website error", result_count=0)
    db.set_crawl_state("unknown:wechat", "removed source error", result_count=0)
    db.set_crawl_state("jiangnan:wechat", "current wechat error", result_count=0)

    response = TestClient(create_app(settings)).get("/")

    assert response.status_code == 200
    assert "项目总数" in response.text
    assert "来源异常" not in response.text


def test_dashboard_shows_website_only_confirmed_projects(tmp_path):
    settings = load_settings("config.yaml")
    settings.db_path = tmp_path / "web.db"
    db = Database(settings.db_path)
    db.init()
    article_id = db.upsert_article(
        Article(
            source_id="waigaoqiao",
            yard_hint="上海外高桥造船",
            channel="官网",
            title="历史官网项目",
            url="https://example.com/website-only",
            content="历史官网项目开工",
            published_at=date(2026, 6, 1),
            fetched_at=datetime.now(),
            content_hash="website",
        )
    )
    ProjectMerger(db).merge(
        article_id,
        Extraction(
            relevant=True,
            yard="上海外高桥造船",
            owner_project="历史官网船东",
            ship_type="集装箱船",
            current_progress="开工",
            confidence=0.95,
        ),
    )

    response = TestClient(create_app(settings)).get("/")

    assert response.status_code == 200
    assert "历史官网船东" in response.text
    assert "<span>项目总数</span><strong>1</strong>" in response.text


def test_dashboard_all_review_statuses_includes_irrelevant_projects(tmp_path):
    settings = load_settings("config.yaml")
    settings.db_path = tmp_path / "web.db"
    db = Database(settings.db_path)
    db.init()

    merger = ProjectMerger(db)
    for index, status in enumerate(["已确认", "待复核", "可能重复", "无关"], start=1):
        article_id = db.upsert_article(
            Article(
                source_id="yard",
                yard_hint="测试船厂",
                channel="微信公众号",
                title=f"{status}项目",
                url=f"https://example.com/{index}",
                content=f"{status}项目开工",
                published_at=date(2026, 6, index),
                fetched_at=datetime.now(),
                content_hash=status,
            )
        )
        merger.merge(
            article_id,
            Extraction(
                relevant=True,
                yard="测试船厂",
                owner_project=f"{status}船东",
                ship_type="集装箱船",
                current_progress="开工",
                confidence=0.95,
                review_status=status,
            ),
        )

    client = TestClient(create_app(settings))
    all_statuses = client.get("/?review_status=")
    irrelevant_only = client.get("/?review_status=无关")

    assert all_statuses.status_code == 200
    assert "当前显示 4 条" in all_statuses.text
    assert all(f"{status}船东" in all_statuses.text for status in ["已确认", "待复核", "可能重复", "无关"])
    assert "当前显示 1 条" in irrelevant_only.text
    assert "无关船东" in irrelevant_only.text


def test_sources_open_link_uses_wechat_target_url(tmp_path):
    settings = load_settings("config.yaml")
    settings.db_path = tmp_path / "web.db"
    db = Database(settings.db_path)
    db.init()
    captcha_url = (
        "https://mp.weixin.qq.com/mp/wappoc_appmsgcaptcha?poc_token=abc"
        "&target_url=https%3A%2F%2Fmp.weixin.qq.com%2Fs%2FgZULykpwdtdBLiorj5u-uQ"
    )
    db.upsert_article(
        Article(
            source_id="zpmc",
            yard_hint="上海振华重工",
            channel="微信公众号",
            title="上海振华重工集团“林鸣院士工作室”揭牌成立",
            url=captcha_url,
            content="",
            published_at=date(2026, 7, 7),
            fetched_at=datetime.now(),
            fetch_status="partial",
            fetch_error="微信验证码/反爬导致正文不可读",
        )
    )

    response = TestClient(create_app(settings)).get("/sources?status=partial")

    assert response.status_code == 200
    assert 'class="mobile-list"' in response.text
    assert 'class="mobile-card"' in response.text
    assert "https://mp.weixin.qq.com/s/gZULykpwdtdBLiorj5u-uQ" in response.text
    assert "wappoc_appmsgcaptcha" not in response.text


def test_sources_hide_obsolete_wechat_captcha_duplicate(tmp_path):
    settings = load_settings("config.yaml")
    settings.db_path = tmp_path / "web.db"
    db = Database(settings.db_path)
    db.init()
    target_url = "https://mp.weixin.qq.com/s/EkIKcASzMh9hS1jvjc-yPQ"
    captcha_url = (
        "https://mp.weixin.qq.com/mp/wappoc_appmsgcaptcha?poc_token=abc"
        "&target_url=https%3A%2F%2Fmp.weixin.qq.com%2Fs%2FEkIKcASzMh9hS1jvjc-yPQ"
    )
    db.upsert_article(
        Article(
            source_id="ship-offshore",
            yard_hint="船海装备网",
            channel="微信公众号",
            title="旧验证码记录",
            url=captcha_url,
            content="",
            published_at=date(2026, 6, 30),
            fetched_at=datetime.now(),
            fetch_status="partial",
            fetch_error="正文过短，可能受页面脚本或验证限制",
        )
    )
    db.upsert_article(
        Article(
            source_id="ship-offshore",
            yard_hint="船海装备网",
            channel="微信公众号",
            title="真实原文记录",
            url=target_url,
            content="正文" * 80,
            published_at=date(2026, 6, 30),
            fetched_at=datetime.now(),
            fetch_status="ok",
            content_hash="ok",
        )
    )

    partial_page = TestClient(create_app(settings)).get("/sources?status=partial")
    ok_page = TestClient(create_app(settings)).get("/sources?status=ok")

    assert partial_page.status_code == 200
    assert "旧验证码记录" not in partial_page.text
    assert ok_page.status_code == 200
    assert "真实原文记录" in ok_page.text


def test_dashboard_defaults_to_confirmed_projects_and_scopes_progress_options(tmp_path):
    settings = load_settings("config.yaml")
    settings.db_path = tmp_path / "web.db"
    db = Database(settings.db_path)
    db.init()
    confirmed_id = db.upsert_article(
        Article(
            source_id="yard",
            yard_hint="测试船厂",
            channel="微信公众号",
            title="确认项目",
            url="https://example.com/confirmed",
            content="确认项目开工",
            published_at=date(2026, 6, 1),
            fetched_at=datetime.now(),
            content_hash="confirmed",
        )
    )
    pending_id = db.upsert_article(
        Article(
            source_id="yard",
            yard_hint="测试船厂",
            channel="微信公众号",
            title="待复核项目",
            url="https://example.com/pending",
            content="待复核项目命名",
            published_at=date(2026, 6, 2),
            fetched_at=datetime.now(),
            content_hash="pending",
        )
    )
    ProjectMerger(db).merge(
        confirmed_id,
        Extraction(
            relevant=True,
            yard="测试船厂",
            owner_project="确认船东",
            ship_type="散货船",
            current_progress="开工",
            confidence=0.95,
        ),
    )
    ProjectMerger(db).merge(
        pending_id,
        Extraction(
            relevant=True,
            yard="测试船厂",
            owner_project="待复核船东",
            ship_type="油船",
            current_progress="命名",
            confidence=0.5,
            review_reason="低置信度",
        ),
    )

    page = TestClient(create_app(settings)).get("/")

    assert page.status_code == 200
    assert "当前显示 1 条" in page.text
    assert "确认船东" in page.text
    assert "待复核船东" not in page.text
    assert "<span>已确认</span>" in page.text
    assert "<span>待复核</span>" in page.text
    assert "<span>可能重复</span>" in page.text
    assert "<span>无关</span>" in page.text
    assert 'option value="已确认" selected' in page.text
    assert 'option value="可能重复"' in page.text
    assert 'option value="无关"' in page.text
    assert 'option value="开工"' in page.text
    assert 'option value="命名"' not in page.text
