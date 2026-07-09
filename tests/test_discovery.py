from datetime import date, datetime

from shipwatch.config import load_settings
from shipwatch.db import Database
from shipwatch.discovery import WechatDiscovery
from shipwatch.domain import ArticleCandidate
from shipwatch.fetch import Fetcher
from shipwatch.pipeline import Pipeline


def test_discovery_cursor_keeps_second_level_timestamp():
    published_at_ts = datetime(2026, 7, 9, 8, 30, 15)
    cursor = Pipeline._discovery_cursor(
        [
            ArticleCandidate(
                source_id="cssc_group",
                yard_hint="中国船舶",
                channel="微信公众号",
                title="新文章",
                url="https://mp.weixin.qq.com/s/latest",
                published_at=published_at_ts.date(),
                published_at_ts=published_at_ts,
            )
        ]
    )

    assert cursor == {
        "last_seen_published_at": "2026-07-09",
        "last_seen_published_at_ts": "2026-07-09T08:30:15",
        "last_seen_url": "https://mp.weixin.qq.com/s/latest",
    }


def test_dajiala_discovery_stops_after_five_existing_old_articles(tmp_path, monkeypatch):
    settings = load_settings("config.yaml")
    settings.db_path = tmp_path / "shipwatch.db"
    settings.app.discovery_stop_existing_count = 5
    db = Database(settings.db_path)
    db.init()
    source = settings.source_by_id("cssc_group")
    account = source.wechat.account_names[0]
    old_ts = datetime(2026, 7, 9, 7, 0, 0)
    old_urls = [f"https://mp.weixin.qq.com/s/old-{index}" for index in range(5)]
    for url in old_urls:
        db.upsert_discovered_article(
            ArticleCandidate(
                source_id=source.id,
                yard_hint=source.yard,
                channel="微信公众号",
                title=url.rsplit("/", 1)[-1],
                url=url,
                published_at=old_ts.date(),
                published_at_ts=old_ts,
                account_name=account,
            )
        )
    db.set_crawl_state(
        f"{source.id}:wechat",
        result_count=5,
        cursor={
            "last_seen_published_at": "2026-07-09",
            "last_seen_published_at_ts": "2026-07-09T08:00:00",
            "last_seen_url": old_urls[0],
        },
    )
    calls = []

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            page = calls[-1]
            if page == 1:
                return {
                    "code": 0,
                    "data": [
                        {
                            "url": url,
                            "title": f"旧文 {index}",
                            "post_time": int(old_ts.timestamp()),
                        }
                        for index, url in enumerate(old_urls)
                    ],
                }
            return {
                "code": 0,
                "data": [
                    {
                        "url": "https://mp.weixin.qq.com/s/should-not-fetch",
                        "title": "不应翻到第二页",
                        "post_time": int(old_ts.timestamp()),
                    }
                ],
            }

    def fake_post(url, json, timeout):
        calls.append(json["page"])
        return Response()

    monkeypatch.setattr("httpx.post", fake_post)

    fetcher = Fetcher(settings.app.user_agent)
    try:
        results = WechatDiscovery(
            settings,
            fetcher,
            dajiala_api_key="test-key",
            db=db,
        ).discover(source, date(2026, 7, 1))
    finally:
        fetcher.close()

    assert calls == [1]
    assert len(results) == 5
    usage = db.api_usage_summary(days=1)
    assert usage[0]["request_meta"] == "page=1"
