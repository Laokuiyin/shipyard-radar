from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from pathlib import Path

from shipwatch.collector import Collector
from shipwatch.config import Settings
from shipwatch.db import Database
from shipwatch.discovery import WechatDiscovery, default_since
from shipwatch.domain import ArticleCandidate
from shipwatch.exporter import ExcelExporter, daily_output_path
from shipwatch.extract import HybridExtractor
from shipwatch.fetch import Fetcher
from shipwatch.merge import ProjectMerger

logger = logging.getLogger(__name__)


class Pipeline:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = Database(settings.db_path)
        self.db.init()

    def discover_and_collect(
        self, since: date | None = None, websites: bool = True, wechat: bool = True,
        source_ids: list[str] | None = None
    ) -> dict[str, int]:
        since = since or default_since(self.settings)
        fetcher = Fetcher(
            self.settings.app.user_agent,
            self.settings.app.request_timeout_seconds,
            self.settings.app.request_delay_seconds,
        )
        collected = 0
        discovered = 0
        queued = 0
        try:
            collectors = []
            if wechat:
                collectors.append(
                    (
                        "wechat",
                        WechatDiscovery(
                            self.settings,
                            fetcher,
                            dajiala_api_key=self.settings.dajiala_api_key,
                            db=self.db,
                        ),
                    )
                )
            article_collector = Collector(self.settings, self.db, fetcher, dajiala_api_key=self.settings.dajiala_api_key)
            seen: set[str] = set()
            sources = [s for s in self.settings.sources if s.id in source_ids] if source_ids else self.settings.sources
            for source in sources:
                for channel_name, discovery in collectors:
                    if channel_name == "wechat" and source.wechat is None:
                        continue
                    source_key = f"{source.id}:{channel_name}"
                    try:
                        candidates = discovery.discover(source, since)
                        discovered += len(candidates)
                        for candidate in candidates:
                            if candidate.url in seen:
                                continue
                            seen.add(candidate.url)
                            self.db.upsert_discovered_article(candidate)
                            queued += 1
                        self.db.set_crawl_state(
                            source_key,
                            None,
                            result_count=len(candidates),
                            cursor=self._discovery_cursor(candidates),
                        )
                    except Exception as exc:
                        logger.exception("来源失败 %s", source_key)
                        self.db.set_crawl_state(source_key, str(exc), result_count=0)
            for row in self.db.pending_body_fetch_articles(
                limit=self.settings.app.max_articles_per_run,
                source_ids=source_ids,
            ):
                article_collector.collect(self._candidate_from_row(row))
                collected += 1
        finally:
            fetcher.close()
        return {"discovered": discovered, "queued": queued, "collected": collected}

    def add_url(self, url: str, source_id: str, title: str = "人工补充链接") -> int:
        source = self.settings.source_by_id(source_id)
        if "weixin.qq.com" not in url:
            raise ValueError("已关闭官网采集，人工补充仅支持微信公众号原文链接")
        channel = "微信公众号"
        fetcher = Fetcher(
            self.settings.app.user_agent,
            self.settings.app.request_timeout_seconds,
            self.settings.app.request_delay_seconds,
        )
        try:
            return Collector(self.settings, self.db, fetcher, dajiala_api_key=self.settings.dajiala_api_key).collect(
                ArticleCandidate(source.id, source.yard, channel, title, url)
            )
        finally:
            fetcher.close()

    def extract_pending(self) -> dict[str, int]:
        extractor = HybridExtractor(self.settings)
        merger = ProjectMerger(self.db)
        processed = relevant = 0
        for row in self.db.pending_articles():
            try:
                published = date.fromisoformat(row["published_at"]) if row["published_at"] else None
                result = extractor.extract(
                    title=row["title"],
                    content=row["content"],
                    yard_hint=row["yard_hint"],
                    published_at=published,
                )
                self.db.mark_extracted(row["id"], result)
                merger.merge(row["id"], result)
                processed += 1
                relevant += int(result.relevant)
            except Exception as exc:
                logger.exception("抽取失败 article_id=%s", row["id"])
                self.db.mark_extraction_error(row["id"], str(exc))
        return {"processed": processed, "relevant": relevant}

    def reprocess_start_projects(self, limit: int | None = None) -> dict[str, int]:
        """Refresh and re-extract the legacy WeChat articles previously marked 开工."""
        fetcher = Fetcher(
            self.settings.app.user_agent,
            self.settings.app.request_timeout_seconds,
            self.settings.app.request_delay_seconds,
        )
        collector = Collector(self.settings, self.db, fetcher, dajiala_api_key=self.settings.dajiala_api_key)
        extractor = HybridExtractor(self.settings)
        merger = ProjectMerger(self.db)
        result = {"selected": 0, "refreshed": 0, "relevant": 0, "irrelevant": 0, "errors": 0}
        try:
            for source_row in self.db.start_project_articles(limit):
                result["selected"] += 1
                try:
                    article_id = collector.collect(self._candidate_from_row(source_row), refresh=True)
                    row = self.db.article_by_id(article_id)
                    if not row or row["fetch_status"] != "ok":
                        raise RuntimeError("纯文本正文获取失败")
                    result["refreshed"] += 1
                    published = date.fromisoformat(row["published_at"]) if row["published_at"] else None
                    extraction = extractor.extract(
                        title=row["title"],
                        content=row["content"],
                        yard_hint=row["yard_hint"],
                        published_at=published,
                    )
                    self.db.mark_extracted(article_id, extraction)
                    if extraction.relevant:
                        merger.merge(article_id, extraction)
                        result["relevant"] += 1
                    else:
                        self.db.mark_start_projects_irrelevant(article_id, extraction.review_reason)
                        result["irrelevant"] += 1
                except Exception:
                    logger.exception("重跑开工记录失败 article_id=%s", source_row["id"])
                    result["errors"] += 1
        finally:
            fetcher.close()
        return result

    @staticmethod
    def _candidate_from_row(row) -> ArticleCandidate:
        published_ts = datetime.fromisoformat(row["published_at_ts"]) if row["published_at_ts"] else None
        return ArticleCandidate(
            source_id=row["source_id"],
            yard_hint=row["yard_hint"],
            channel=row["channel"],
            title=row["title"],
            url=row["url"],
            published_at=date.fromisoformat(row["published_at"])
            if row["published_at"]
            else None,
            published_at_ts=published_ts,
            account_name=row["account_name"],
        )

    @staticmethod
    def _discovery_cursor(candidates: list[ArticleCandidate]) -> dict | None:
        if not candidates:
            return None
        timed = [candidate for candidate in candidates if candidate.published_at_ts]
        dated = [candidate for candidate in candidates if candidate.published_at]
        if timed:
            latest = max(timed, key=lambda item: item.published_at_ts)
            return {
                "last_seen_published_at": latest.published_at.isoformat()
                if latest.published_at
                else None,
                "last_seen_published_at_ts": latest.published_at_ts.isoformat()
                if latest.published_at_ts
                else None,
                "last_seen_url": latest.url,
            }
        if not dated:
            return {"last_seen_url": candidates[0].url}
        latest = max(dated, key=lambda item: item.published_at)
        return {
            "last_seen_published_at": latest.published_at.isoformat(),
            "last_seen_url": latest.url,
        }

    def export(self, output_path: Path | None = None) -> Path:
        output_path = output_path or daily_output_path(self.settings.output_dir)
        start = datetime.combine(date.today(), time.min).isoformat()
        source_names = {source.id: source.yard for source in self.settings.sources}
        source_channels = {}
        for source in self.settings.sources:
            source_channels[source.id] = [("wechat", "微信公众号")] if source.wechat else []
        return ExcelExporter(self.db, source_names, source_channels).export(
            output_path, changed_since=start
        )

    def run_daily(self) -> tuple[dict[str, int], dict[str, int]]:
        since = date.today() - timedelta(days=14)
        collected = self.discover_and_collect(since=since)
        extracted = self.extract_pending()
        return collected, extracted
