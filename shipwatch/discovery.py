from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from shipwatch.config import Settings, SourceConfig
from shipwatch.db import Database
from shipwatch.domain import ArticleCandidate
from shipwatch.fetch import Fetcher
from shipwatch.text import normalize_url

logger = logging.getLogger(__name__)

class WechatDiscovery:
    DAJIALA_API_URL = "https://www.dajiala.com/fbmain/monitor/v3/post_history"
    def __init__(
        self,
        settings: Settings,
        fetcher: Fetcher,
        dajiala_api_key: str | None = None,
        db: Database | None = None,
    ):
        self.settings = settings
        self.fetcher = fetcher
        self.dajiala_api_key = dajiala_api_key
        self.db = db

    def discover(self, source: SourceConfig, since: date) -> list[ArticleCandidate]:
        if source.wechat is None:
            return []
        if self.dajiala_api_key:
            try:
                return self._discover_via_dajiala(source, since)
            except Exception as exc:
                logger.warning("打价啦 API 公众号发现失败 %s: %s, 回退到搜狗", source.id, exc)
        return self._discover_via_sogou(source, since)

    def _discover_via_dajiala(self, source: SourceConfig, since: date) -> list[ArticleCandidate]:
        import httpx
        results: dict[str, ArticleCandidate] = {}
        cursor = self.db.crawl_cursor(f"{source.id}:wechat") if self.db else {}
        if not cursor and self.db:
            cursor = self.db.latest_source_cursor(source.id, "微信公众号")
        is_new_source = not cursor
        cursor_dt = self._cursor_datetime(cursor)
        cursor_date = self._cursor_date(cursor)
        consecutive_existing = 0
        stop_after = self.settings.app.discovery_stop_existing_count
        max_pages = 1 if is_new_source else min(self.settings.app.max_list_pages_per_source, 10)
        accounts = [account.strip() for account in source.wechat.account_names[:1] if account.strip()]
        for account in accounts:
            page = 1
            while page <= max_pages:
                resp = None
                try:
                    resp = httpx.post(
                        self.DAJIALA_API_URL,
                        json={"name": account, "page": page, "key": self.dajiala_api_key},
                        timeout=self.settings.app.request_timeout_seconds,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                except Exception as exc:
                    if self.db:
                        self.db.record_api_call(
                            "dajiala",
                            "post_history",
                            source_id=source.id,
                            account_name=account,
                            request_meta=f"page={page}",
                            success=False,
                            status_code=resp.status_code if resp is not None else None,
                            error=str(exc),
                        )
                    logger.warning("打价啦 API 请求失败 %s page=%s: %s", account, page, exc)
                    break
                if self.db:
                    self.db.record_api_call(
                        "dajiala",
                        "post_history",
                        source_id=source.id,
                        account_name=account,
                        request_meta=f"page={page}",
                        success=data.get("code") == 0,
                        status_code=resp.status_code,
                        error=None if data.get("code") == 0 else str(data.get("msg")),
                    )
                if data.get("code") != 0:
                    logger.warning("打价啦 API 返回异常: %s", data.get("msg"))
                    break
                articles = data.get("data") or []
                if not articles:
                    break
                for item in articles:
                    url = normalize_url(item.get("url", ""))
                    if not url:
                        continue
                    title = (item.get("title") or "").strip()
                    if not title:
                        continue
                    post_time = item.get("post_time")
                    published = None
                    published_ts = None
                    if post_time:
                        published_ts = datetime.fromtimestamp(post_time)
                        published = published_ts.date()
                    exists = self.db.article_exists(url) if self.db else False
                    if exists and self._at_or_before_cursor(published_ts, published, cursor_dt, cursor_date):
                        consecutive_existing += 1
                    else:
                        consecutive_existing = 0
                    results[url] = ArticleCandidate(
                        source_id=source.id,
                        yard_hint=source.yard,
                        channel="微信公众号",
                        title=title,
                        url=url,
                        published_at=published,
                        published_at_ts=published_ts,
                        account_name=account,
                    )
                    if stop_after and consecutive_existing >= stop_after:
                        return list(results.values())
                page += 1
        return list(results.values())

    def _discover_via_sogou(self, source: SourceConfig, since: date) -> list[ArticleCandidate]:
        logger.warning("搜狗微信发现未启用，来源 %s 本轮仅保留打价啦结果", source.id)
        return []

    @staticmethod
    def _cursor_date(cursor: dict) -> date | None:
        raw = cursor.get("last_seen_published_at")
        if not raw:
            return None
        try:
            return date.fromisoformat(raw)
        except ValueError:
            return None

    @staticmethod
    def _cursor_datetime(cursor: dict) -> datetime | None:
        raw = cursor.get("last_seen_published_at_ts")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

    @staticmethod
    def _at_or_before_cursor(
        published_ts: datetime | None,
        published: date | None,
        cursor_dt: datetime | None,
        cursor_date: date | None,
    ) -> bool:
        if cursor_dt and published_ts:
            return published_ts <= cursor_dt
        if cursor_date is None:
            return True
        return bool(published and published <= cursor_date)


def default_since(settings):
    from datetime import date, timedelta
    return date.today() - timedelta(days=settings.app.lookback_days)
