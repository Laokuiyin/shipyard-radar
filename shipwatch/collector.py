from __future__ import annotations

import re
import logging
from datetime import date, datetime
from urllib.parse import urlsplit

import httpx

from shipwatch.config import Settings
from shipwatch.db import Database
from shipwatch.domain import Article, ArticleCandidate
from shipwatch.fetch import Fetcher
from shipwatch.parsers import extract_web_article, extract_wechat_article, soup
from shipwatch.text import clean_text, normalize_url, sha256_text

logger = logging.getLogger(__name__)


class Collector:
    DAJIALA_TEXT_API_URL = "https://www.dajiala.com/fbmain/monitor/v3/article_detail"
    DAJIALA_HTML_API_URL = "https://www.dajiala.com/fbmain/monitor/v3/article_html"

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
            published_ts = candidate.published_at_ts
            account = candidate.account_name

            # 如果配置了打价啦 API 且是公众号文章，优先使用 API
            if self._has_api and candidate.channel == "微信公众号":
                # 检查数据库是否已有正文，避免重复花钱调用 API
                existing = self.db.query(
                    """
                    SELECT id, title, content, published_at, published_at_ts, account_name
                    FROM articles
                    WHERE url=? AND fetch_status='ok' AND length(content) >= 80
                    """,
                    (candidate.url,),
                )
                if existing:
                    row = existing[0]
                    title = row["title"] or candidate.title
                    content = row["content"] or ""
                    published = date.fromisoformat(row["published_at"]) if row["published_at"] else candidate.published_at
                    published_ts = (
                        datetime.fromisoformat(row["published_at_ts"])
                        if row["published_at_ts"]
                        else candidate.published_at_ts
                    )
                    account = row["account_name"] or candidate.account_name
                    final_url = candidate.url
                    if content:
                        api_used = True
                if not api_used:
                    try:
                        title, content, published, published_ts, account, final_url = (
                            self._fetch_text_via_dajiala(
                                candidate.url,
                                source_id=candidate.source_id,
                                account_name=candidate.account_name,
                            )
                        )
                        if not title:
                            title = candidate.title
                        if content:
                            api_used = True
                    except Exception as exc:
                        logger.warning("打价啦 API 纯文本获取失败 %s: %s, 回退到 HTML 接口", candidate.url, exc)
                        try:
                            title, content, published, published_ts, account, final_url = (
                                self._fetch_html_via_dajiala(
                                    candidate.url,
                                    source_id=candidate.source_id,
                                    account_name=candidate.account_name,
                                )
                            )
                            if not title:
                                title = candidate.title
                            if content:
                                api_used = True
                        except Exception as html_exc:
                            logger.warning(
                                "打价啦 API HTML 获取失败 %s: %s, 回退到直接抓取",
                                candidate.url,
                                html_exc,
                            )

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
                    published_ts = published_ts or candidate.published_at_ts
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
                published_at_ts=published_ts or candidate.published_at_ts,
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
                published_at_ts=candidate.published_at_ts,
                fetched_at=now,
                account_name=candidate.account_name,
                fetch_status="blocked" if self.is_restricted_error(error) else "error",
                fetch_error=error,
            )
        self.last_fetch_status = article.fetch_status
        self.last_fetch_error = article.fetch_error
        return self.db.upsert_article(article)

    def _article_url(self, article_url: str) -> str:
        """Resolve the original WeChat URL from a captcha redirect."""
        from urllib.parse import parse_qs, unquote, urlparse

        parsed = urlparse(article_url)
        if "appmsgcaptcha" not in parsed.path:
            return article_url
        target = parse_qs(parsed.query).get("target_url", [None])[0]
        return unquote(target) if target else article_url

    def _fetch_text_via_dajiala(
        self,
        article_url: str,
        source_id: str | None = None,
        account_name: str | None = None,
    ) -> tuple[str, str, date | None, datetime | None, str | None, str]:
        """Fetch clean article text from article_detail before trying HTML."""
        article_url = self._article_url(article_url)
        resp = None
        try:
            resp = httpx.get(
                self.DAJIALA_TEXT_API_URL,
                params={"url": article_url, "key": self.dajiala_api_key},
                timeout=self.settings.app.request_timeout_seconds,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            self.db.record_api_call(
                "dajiala", "article_detail", source_id=source_id, account_name=account_name,
                article_url=article_url, success=False,
                status_code=resp.status_code if resp is not None else None, error=str(exc),
            )
            raise
        self.db.record_api_call(
            "dajiala", "article_detail", source_id=source_id, account_name=account_name,
            article_url=article_url, success=data.get("code") == 0, status_code=resp.status_code,
            error=None if data.get("code") == 0 else str(data.get("msg", data.get("msk", ""))),
        )
        if data.get("code") != 0:
            raise RuntimeError(f"打价啦 API 返回异常: {data.get('msg', data.get('msk', ''))}")
        article_data = data.get("data") or data
        content = clean_text(article_data.get("content") or "")
        if not content:
            raise RuntimeError("打价啦 API 未返回纯文本正文")
        return self._article_fields(article_data, content, article_url)

    def _fetch_html_via_dajiala(
        self,
        article_url: str,
        source_id: str | None = None,
        account_name: str | None = None,
    ) -> tuple[str, str, date | None, datetime | None, str | None, str]:
        """Fallback for article_detail: fetch HTML and convert it to plain text."""
        article_url = self._article_url(article_url)
        resp = None
        try:
            resp = httpx.post(
                self.DAJIALA_HTML_API_URL,
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
        html_content = article_data.get("html") or article_data.get("content_multi_text") or ""
        content = self._plain_text_from_html(html_content)
        if not content:
            raise RuntimeError("打价啦 API 未返回可读 HTML 正文")
        return self._article_fields(article_data, content, article_url)

    @staticmethod
    def _plain_text_from_html(html_content: str) -> str:
        doc = soup(html_content)
        for element in doc.select("script, style, img, svg, video, audio, noscript"):
            element.decompose()
        return clean_text(doc.get_text("\n", strip=True))

    @staticmethod
    def _article_fields(
        article_data: dict,
        content: str,
        article_url: str,
    ) -> tuple[str, str, date | None, datetime | None, str | None, str]:
        title = (article_data.get("title") or "").strip()
        nickname = article_data.get("nickname") or article_data.get("nick_name") or None
        post_time = article_data.get("post_time") or article_data.get("pubtime")
        published = None
        published_ts = None
        if post_time:
            published_ts = datetime.fromtimestamp(int(post_time))
            published = published_ts.date()
        return title, content, published, published_ts, nickname, article_url
    @staticmethod
    def _sogou_target(html: str) -> str | None:
        import re

        parts = re.findall(r"url\s*\+=\s*['\"]([^'\"]+)['\"]", html)
        target = "".join(parts).replace("@", "")
        if target.startswith("https://mp.weixin.qq.com/"):
            return target
        return None
