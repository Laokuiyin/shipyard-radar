from datetime import datetime

from shipwatch.collector import Collector
from shipwatch.config import load_settings
from shipwatch.db import Database
from shipwatch.fetch import Fetcher


class Response:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "code": 0,
            "data": {
                "title": "新船开工",
                "content": "\n  这是接口返回的纯文本正文。\n",
                "nick_name": "沪东中华造船",
                "pubtime": 1780848000,
            },
        }


def test_article_detail_uses_clean_text_response(tmp_path, monkeypatch):
    settings = load_settings("config.yaml")
    settings.db_path = tmp_path / "shipwatch.db"
    db = Database(settings.db_path)
    db.init()
    calls = []

    def fake_get(url, params, timeout):
        calls.append((url, params, timeout))
        return Response()

    monkeypatch.setattr("httpx.get", fake_get)
    fetcher = Fetcher(settings.app.user_agent)
    try:
        collector = Collector(settings, db, fetcher, dajiala_api_key="test-key")
        title, content, published, published_ts, account, url = collector._fetch_text_via_dajiala(
            "https://mp.weixin.qq.com/s/test",
            source_id="hudong_zhonghua",
            account_name="沪东中华造船",
        )
    finally:
        fetcher.close()

    assert calls[0][0] == Collector.DAJIALA_TEXT_API_URL
    assert calls[0][1] == {"url": "https://mp.weixin.qq.com/s/test", "key": "test-key"}
    assert title == "新船开工"
    assert content == "这是接口返回的纯文本正文。"
    assert account == "沪东中华造船"
    assert published == datetime.fromtimestamp(1780848000).date()
    assert published_ts == datetime.fromtimestamp(1780848000)
    assert url == "https://mp.weixin.qq.com/s/test"


def test_html_fallback_removes_markup_and_images():
    content = Collector._plain_text_from_html(
        "<style>body { color: red; }</style><p>船舶<strong>正式开工</strong></p><img src='x'><script>x()</script>"
    )
    assert "".join(content.split()) == "船舶正式开工"


def test_article_detail_accepts_string_pubtime():
    _, _, published, published_ts, _, _ = Collector._article_fields(
        {"title": "新船开工", "content": "正文", "pubtime": "2026-07-11 17:28:19"},
        "正文",
        "https://mp.weixin.qq.com/s/test",
    )
    assert published == datetime(2026, 7, 11).date()
    assert published_ts == datetime(2026, 7, 11, 17, 28, 19)
