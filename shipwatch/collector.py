from __future__ import annotations

import re
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
    DAJIALA_API_URL = "https://www.dajiala.com/fbmain/monitor/v3/article_html"

    def __init__(self, settings: Settings, db: Database, fetcher: Fetcher, dajiala_api_key: str | None = None):
        self.settings = settings
        self.db = db
        self.fetcher = fetcher
        self.dajiala_api_key = dajiala_api_key
        self._has_api = bool(dajiala_api_key)
        self.last_fetch_status: str | None = None
        self.last_fetch_error: str | None = None

    def collect(self, candidate: ArticleCandidate) -> int:
        now = datetime.now()
        self.last_fetch_status = None
        self.last_fetch_error = None
        try:
            api_used = False
            title = content = ""
            published = candidate.published_at
            account = candidate.account_name

            # 如果配置了打价啦 API 且是公众号文章，优先使用 API
            if self._has_api and candidate.channel == "微信公众号":
                # 检查数据库是否已有正文，避免重复花钱调用 API
                existing = self.db.query(
                    """
                    SELECT id, title, content, published_at, account_name
                    FROM articles
                    WHERE url=? AND fetch_status='ok' AND length(content) >= 80
                    """,
                    (candidate.url,),
                )
                if existing:
                    row = existing[0]
                    title = row["title"] or candidate.title
                    content = row["content"] or ""
                    published = row["published_at"] or candidate.published_at
                    account = row["account_name"] or candidate.account_name
                    final_url = candidate.url
                    if content:
                        api_used = True
                if not api_used:
                    try:
                        title, content, published, account, final_url = self._fetch_via_dajiala(
                            candidate.url,
                            source_id=candidate.source_id,
                            account_name=candidate.account_name,
                        )
                        if not title:
                            title = candidate.title
                        if content:
                            api_used = True
                    except Exception as exc:
                        logger.warning("打价啦 API 正文获取失败 %s: %s, 回退到直接抓取", candidate.url, exc)

            if not api_used:
                response = self.fetcher.get(candidate.url)
                raw_final_url = response.url
                final_url = normalize_url(response.url)
                restricted_hint = False
                if candidate.channel == "微信公众号":
                    if "appmsgcaptcha" in raw_final_url:
                        restricted_hint = True
                    resolved_sogou_url = None
                    if "weixin.sogou.com" in (urlsplit(final_url).hostname or ""):
                        resolved_sogou_url = self._sogou_target(response.text)
                        if resolved_sogou_url:
                            response = self.fetcher.get(resolved_sogou_url)
                            final_url = normalize_url(response.url)
                            if "appmsgcaptcha" in final_url:
                                restricted_hint = True
                                final_url = normalize_url(resolved_sogou_url)
                    if "mp.weixin.qq.com" not in (urlsplit(final_url).hostname or ""):
                        raise ValueError(f"搜狗链接未跳转到微信原文: {final_url}")
                    title, content, published, account = extract_wechat_article(response.text)
                    source_config = self.settings.source_by_id(candidate.source_id)
                    allowed = source_config.wechat.account_names if source_config.wechat else []
                    if account and not any(name in account or account in name for name in allowed):
                        raise ValueError(f"公众号不在白名单: {account}")
                else:
                    title, content, published = extract_web_article(response.text, final_url)
                    account = None

            if len(content) < 80:
                status = "partial"
                if not api_used and candidate.channel == "微信公众号" and restricted_hint:
                    error = "微信验证码/反爬导致正文不可读"
                else:
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
            error = str(exc)
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
                fetch_status="blocked" if self.is_restricted_error(error) else "error",
                fetch_error=error,
            )
        self.last_fetch_status = article.fetch_status
        self.last_fetch_error = article.fetch_error
        return self.db.upsert_article(article)

    def _fetch_via_dajiala(
        self,
        article_url: str,
        source_id: str | None = None,
        account_name: str | None = None,
    ) -> tuple[str, str, datetime.date | None, str | None, str]:
        """通过打价啦 API 获取文章正文 HTML，返回 (title, content, published, account, final_url)"""
        import httpx
        from urllib.parse import urlparse, parse_qs, unquote
        # 如果链接是验证码跳转，从中提取原始文章链接
        parsed = urlparse(article_url)
        if "appmsgcaptcha" in parsed.path:
            qs = parse_qs(parsed.query)
            target = qs.get("target_url", [None])[0]
            if target:
                article_url = unquote(target)
        resp = None
        try:
            resp = httpx.post(
                self.DAJIALA_API_URL,
                json={"url": article_url, "key": self.dajiala_api_key},
                timeout=self.settings.app.request_timeout_seconds,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            self.db.record_api_call(
                "dajiala",
                "article_html",
                source_id=source_id,
                account_name=account_name,
                article_url=article_url,
                success=False,
                status_code=resp.status_code if resp is not None else None,
                error=str(exc),
            )
            raise
        self.db.record_api_call(
            "dajiala",
            "article_html",
            source_id=source_id,
            account_name=account_name,
            article_url=article_url,
            success=data.get("code") == 0,
            status_code=resp.status_code,
            error=None if data.get("code") == 0 else str(data.get("msg", data.get("msk", ""))),
        )
        if data.get("code") != 0:
            raise RuntimeError(f"打价啦 API 返回异常: {data.get('msg', data.get('msk', ''))}")
        article_data = data.get("data") or {}
        html_content = article_data.get("html") or ""
        # Strip style tags, images, outer wrappers from wechat HTML
        if html_content:
            html_content = re.sub(r"<style[^>]*>.*?</style>", "", html_content, flags=re.DOTALL)
            html_content = re.sub(r"<\?xml[^>]*\?>", "", html_content)
            html_content = re.sub(r"</?html[^>]*>", "", html_content)
            html_content = re.sub(r"</?head[^>]*>", "", html_content)
            html_content = re.sub(r"<title>.*?</title>", "", html_content, flags=re.DOTALL)
            html_content = re.sub(r"<meta[^>]*>", "", html_content)
            html_content = re.sub(r"</?body[^>]*>", "", html_content)
            html_content = re.sub(r"<img[^>]*>", "", html_content)
            html_content = re.sub(r"<br\s*/?>", "", html_content)
            html_content = re.sub(r"<p[^>]*>\s*</p>", "", html_content)
            html_content = re.sub(r"<section[^>]*>\s*</section>", "", html_content)
            html_content = html_content.strip()
        title = (article_data.get("title") or "").strip()
        nickname = article_data.get("nickname") or None
        post_time = article_data.get("post_time")
        published = None
        if post_time:
            from datetime import datetime
            published = datetime.fromtimestamp(post_time).date()
        return title, html_content.strip(), published, nickname, article_url
    @staticmethod
    def _sogou_target(html: str) -> str | None:
        import re

        parts = re.findall(r"url\s*\+=\s*['\"]([^'\"]+)['\"]", html)
        target = "".join(parts).replace("@", "")
        if target.startswith("https://mp.weixin.qq.com/"):
            return target
        return None
