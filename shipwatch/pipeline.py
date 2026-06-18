from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from pathlib import Path

from shipwatch.collector import Collector
from shipwatch.config import Settings
from shipwatch.db import Database
from shipwatch.discovery import WebsiteDiscovery, WechatDiscovery, default_since
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
        self, since: date | None = None, websites: bool = True, wechat: bool = True
    ) -> dict[str, int]:
        since = since or default_since(self.settings)
        fetcher = Fetcher(
            self.settings.app.user_agent,
            self.settings.app.request_timeout_seconds,
            self.settings.app.request_delay_seconds,
        )
        collected = 0
        discovered = 0
        try:
            collectors = []
            if websites:
                collectors.append(("website", WebsiteDiscovery(self.settings, fetcher)))
            if wechat:
                collectors.append(("wechat", WechatDiscovery(self.settings, fetcher)))
            article_collector = Collector(self.settings, self.db, fetcher)
            seen: set[str] = set()
            for source in self.settings.sources:
                for channel_name, discovery in collectors:
                    source_key = f"{source.id}:{channel_name}"
                    try:
                        candidates = discovery.discover(source, since)
                        discovered += len(candidates)
                        for candidate in candidates:
                            if candidate.url in seen:
                                continue
                            seen.add(candidate.url)
                            article_collector.collect(candidate)
                            collected += 1
                            if collected >= self.settings.app.max_articles_per_run:
                                return {"discovered": discovered, "collected": collected}
                        self.db.set_crawl_state(source_key, result_count=len(candidates))
                    except Exception as exc:
                        logger.exception("来源失败 %s", source_key)
                        self.db.set_crawl_state(source_key, str(exc), result_count=0)
        finally:
            fetcher.close()
        return {"discovered": discovered, "collected": collected}

    def add_url(self, url: str, source_id: str, title: str = "人工补充链接") -> int:
        source = self.settings.source_by_id(source_id)
        channel = "微信公众号" if "weixin.qq.com" in url else "官网"
        fetcher = Fetcher(
            self.settings.app.user_agent,
            self.settings.app.request_timeout_seconds,
            self.settings.app.request_delay_seconds,
        )
        try:
            return Collector(self.settings, self.db, fetcher).collect(
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

    def export(self, output_path: Path | None = None) -> Path:
        output_path = output_path or daily_output_path(self.settings.output_dir)
        start = datetime.combine(date.today(), time.min).isoformat()
        source_names = {source.id: source.yard for source in self.settings.sources}
        return ExcelExporter(self.db, source_names).export(output_path, changed_since=start)

    def run_daily(self) -> tuple[dict[str, int], dict[str, int], Path]:
        since = date.today() - timedelta(days=14)
        collected = self.discover_and_collect(since=since)
        extracted = self.extract_pending()
        return collected, extracted, self.export()
