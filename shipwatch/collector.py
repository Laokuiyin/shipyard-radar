from __future__ import annotations

import logging
from datetime import datetime
from urllib.parse import urlsplit

from shipwatch.config import Settings
from shipwatch.db import Database
from shipwatch.domain import Article, ArticleCandidate
from shipwatch.fetch import Fetcher
from shipwatch.parsers import extract_web_article, extract_wechat_article
from shipwatch.text import normalize_url, sha256_text

logger = logging.getLogger(__name__)


class Collector:
    def __init__(self, settings: Settings, db: Database, fetcher: Fetcher):
        self.settings = settings
        self.db = db
        self.fetcher = fetcher

    def collect(self, candidate: ArticleCandidate) -> int:
        now = datetime.now()
        try:
            response = self.fetcher.get(candidate.url)
            final_url = normalize_url(response.url)
            if candidate.channel == "微信公众号":
                resolved_sogou_url = None
                if "weixin.sogou.com" in (urlsplit(final_url).hostname or ""):
                    resolved_sogou_url = self._sogou_target(response.text)
                    if resolved_sogou_url:
                        response = self.fetcher.get(resolved_sogou_url)
                        final_url = normalize_url(response.url)
                        if "appmsgcaptcha" in final_url:
                            final_url = normalize_url(resolved_sogou_url)
                if "mp.weixin.qq.com" not in (urlsplit(final_url).hostname or ""):
                    raise ValueError(f"搜狗链接未跳转到微信原文: {final_url}")
                title, content, published, account = extract_wechat_article(response.text)
                allowed = self.settings.source_by_id(candidate.source_id).wechat.account_names
                if account and not any(name in account or account in name for name in allowed):
                    raise ValueError(f"公众号不在白名单: {account}")
            else:
                title, content, published = extract_web_article(response.text, final_url)
                account = None

            if len(content) < 80:
                status = "partial"
                error = "正文过短，可能受页面脚本或验证限制"
            else:
                status = "ok"
                error = None
            article = Article(
                source_id=candidate.source_id,
                yard_hint=candidate.yard_hint,
                channel=candidate.channel,
                title=title or candidate.title,
                url=final_url,
                content=content,
                published_at=published or candidate.published_at,
                fetched_at=now,
                account_name=account or candidate.account_name,
                fetch_status=status,
                fetch_error=error,
                content_hash=sha256_text(content) if content else "",
            )
        except Exception as exc:
            logger.warning("文章抓取失败 %s: %s", candidate.url, exc)
            article = Article(
                source_id=candidate.source_id,
                yard_hint=candidate.yard_hint,
                channel=candidate.channel,
                title=candidate.title,
                url=normalize_url(candidate.url),
                content="",
                published_at=candidate.published_at,
                fetched_at=now,
                account_name=candidate.account_name,
                fetch_status="blocked" if "验证" in str(exc) else "error",
                fetch_error=str(exc),
            )
        return self.db.upsert_article(article)

    @staticmethod
    def _sogou_target(html: str) -> str | None:
        import re

        parts = re.findall(r"url\s*\+=\s*['\"]([^'\"]+)['\"]", html)
        target = "".join(parts).replace("@", "")
        if target.startswith("https://mp.weixin.qq.com/"):
            return target
        return None
