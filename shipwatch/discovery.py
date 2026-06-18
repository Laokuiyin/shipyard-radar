from __future__ import annotations

import logging
from datetime import date, timedelta
from urllib.parse import quote, urlsplit

from shipwatch.config import Settings, SourceConfig
from shipwatch.domain import ArticleCandidate
from shipwatch.fetch import Fetcher
from shipwatch.parsers import extract_links, looks_like_article, looks_like_list_page, soup
from shipwatch.text import normalize_url, parse_date

logger = logging.getLogger(__name__)


class WebsiteDiscovery:
    def __init__(self, settings: Settings, fetcher: Fetcher):
        self.settings = settings
        self.fetcher = fetcher

    def discover(self, source: SourceConfig, since: date) -> list[ArticleCandidate]:
        host = urlsplit(source.website.base_url).hostname or ""
        queue = list(source.website.seed_urls)
        visited: set[str] = set()
        results: dict[str, ArticleCandidate] = {}
        page_count = 0
        successful_pages = 0
        errors: list[str] = []

        while queue and page_count < self.settings.app.max_list_pages_per_source:
            page_url = normalize_url(queue.pop(0))
            if page_url in visited:
                continue
            visited.add(page_url)
            try:
                page = self.fetcher.get(page_url)
            except Exception as exc:
                logger.warning("官网列表抓取失败 %s: %s", page_url, exc)
                errors.append(f"{page_url}: {exc}")
                continue
            page_count += 1
            successful_pages += 1
            for title, url in extract_links(
                page.text, page.url, {host, "mp.weixin.qq.com"}
            ):
                normalized = normalize_url(url)
                if normalized in visited:
                    continue
                link_host = urlsplit(normalized).hostname or ""
                if link_host == "mp.weixin.qq.com":
                    results[normalized] = ArticleCandidate(
                        source_id=source.id,
                        yard_hint=source.yard,
                        channel="微信公众号",
                        title=title,
                        url=normalized,
                    )
                    continue
                if looks_like_list_page(title, normalized):
                    if len(queue) < self.settings.app.max_list_pages_per_source * 4:
                        queue.append(normalized)
                    continue
                if looks_like_article(title, normalized, self.settings.app.relevance_keywords):
                    published = parse_date(title + " " + normalized)
                    if published and published < since:
                        continue
                    results[normalized] = ArticleCandidate(
                        source_id=source.id,
                        yard_hint=source.yard,
                        channel="官网",
                        title=title,
                        url=normalized,
                        published_at=published,
                    )
                elif len(queue) < self.settings.app.max_list_pages_per_source * 3:
                    if any(word in title for word in ("新闻", "动态", "资讯")):
                        queue.append(normalized)
        if successful_pages == 0:
            raise RuntimeError("；".join(errors) or "官网无可访问页面")
        return list(results.values())


class WechatDiscovery:
    SEARCH_URL = "https://weixin.sogou.com/weixin?type=2&query={query}&page={page}"

    def __init__(self, settings: Settings, fetcher: Fetcher):
        self.settings = settings
        self.fetcher = fetcher

    def discover(self, source: SourceConfig, since: date) -> list[ArticleCandidate]:
        results: dict[str, ArticleCandidate] = {}
        for account in source.wechat.account_names:
            for page_no in range(1, min(self.settings.app.max_list_pages_per_source, 10) + 1):
                url = self.SEARCH_URL.format(query=quote(account), page=page_no)
                try:
                    response = self.fetcher.get(url)
                except Exception as exc:
                    logger.warning("搜狗微信抓取失败 %s: %s", account, exc)
                    break
                if "请输入验证码" in response.text or "antispider" in response.url:
                    logger.warning("搜狗微信触发验证: %s", account)
                    raise RuntimeError(f"搜狗微信触发验证码/反爬验证：{account}")
                found = self._parse_results(response.text, source, account, since)
                for item in found:
                    results[item.url] = item
                if not found:
                    break
        return list(results.values())

    @staticmethod
    def _parse_results(
        html: str, source: SourceConfig, expected_account: str, since: date
    ) -> list[ArticleCandidate]:
        doc = soup(html)
        items: list[ArticleCandidate] = []
        for block in doc.select("li[id^='sogou_vr_'], .news-box li"):
            account_node = block.select_one(".account, .s-p a, .all-time-y2")
            actual_account = (
                account_node.get_text(" ", strip=True) if account_node else expected_account
            )
            if not any(name in actual_account or actual_account in name for name in source.wechat.account_names):
                continue
            anchor = block.select_one("h3 a[href], .txt-box h3 a[href]")
            if not anchor:
                continue
            href = anchor.get("href", "")
            if href.startswith("/link?"):
                href = "https://weixin.sogou.com" + href
            published = parse_date(block.get_text(" ", strip=True))
            if published and published < since:
                continue
            title = anchor.get_text(" ", strip=True)
            if not any(keyword in title + block.get_text(" ", strip=True) for keyword in (
                "船", "项目", "开工", "交付", "签约", "下水", "试航", "龙骨", "出坞"
            )):
                continue
            items.append(
                ArticleCandidate(
                    source_id=source.id,
                    yard_hint=source.yard,
                    channel="微信公众号",
                    title=title,
                    url=href,
                    published_at=published,
                    account_name=actual_account,
                )
            )
        return items


def default_since(settings: Settings) -> date:
    return date.today() - timedelta(days=settings.app.lookback_days)
