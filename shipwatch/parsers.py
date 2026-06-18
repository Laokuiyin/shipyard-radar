from __future__ import annotations

import json
import re
from datetime import date
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from shipwatch.text import clean_text, parse_date


ARTICLE_HINTS = re.compile(
    r"(article|detail|content|info|show|view|/\d{4}[-/]\d{1,2}|/\d{5,}\.html)",
    re.IGNORECASE,
)


def soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def extract_links(
    html: str, page_url: str, allowed_hosts: str | set[str]
) -> list[tuple[str, str]]:
    doc = soup(html)
    hosts = {allowed_hosts} if isinstance(allowed_hosts, str) else allowed_hosts
    links: list[tuple[str, str]] = []
    for anchor in doc.select("a[href]"):
        href = anchor.get("href", "").strip()
        if not href or href.startswith(("javascript:", "#", "mailto:")):
            continue
        url = urljoin(page_url, href)
        if urlsplit(url).hostname not in hosts:
            continue
        title = clean_text(anchor.get("title") or anchor.get_text(" ", strip=True))
        if title:
            links.append((title, url))
    return links


def looks_like_article(title: str, url: str, keywords: list[str]) -> bool:
    return bool(ARTICLE_HINTS.search(url)) or any(word in title for word in keywords)


def looks_like_list_page(title: str, url: str) -> bool:
    generic_titles = {"新闻中心", "集团新闻", "企业新闻", "公司新闻", "新闻动态", "资讯中心", "了解更多>>"}
    lower_url = url.lower()
    return (
        title.strip() in generic_titles
        or any(token in lower_url for token in ("/index.", "/list.", "xwzx", "xwdt"))
        or bool(re.search(r"/(?:news|information)\.html$", url, re.IGNORECASE))
        or "/document/show/" in lower_url
        or bool(re.search(r"/n\d+/index\.html", url, re.IGNORECASE))
    )


def extract_web_article(html: str, url: str) -> tuple[str, str, date | None]:
    doc = soup(html)
    for element in doc.select("script, style, nav, footer, header, form, aside"):
        element.decompose()

    title = ""
    for selector in (
        "meta[property='og:title']",
        "meta[name='ArticleTitle']",
        "h1",
        ".article-title",
        ".news-title",
        "title",
    ):
        node = doc.select_one(selector)
        if node:
            title = clean_text(node.get("content") or node.get_text(" ", strip=True))
            if title:
                break

    published = None
    candidates = [
        node.get("content") or node.get_text(" ", strip=True)
        for node in doc.select(
            "meta[property='article:published_time'], meta[name='PubDate'], "
            "meta[name='publishdate'], time, .date, .time, .publish-time, .article-info"
        )
    ]
    candidates.append(doc.get_text(" ", strip=True)[:1000])
    for value in candidates:
        published = parse_date(value)
        if published:
            break

    best_text = ""
    selectors = (
        "article",
        "#js_content",
        ".article-content",
        ".news-content",
        ".content",
        ".TRS_Editor",
        ".detail",
        "main",
    )
    for selector in selectors:
        for node in doc.select(selector):
            text = clean_text(node.get_text("\n", strip=True))
            if len(text) > len(best_text):
                best_text = text
    if len(best_text) < 150:
        best_text = clean_text(doc.body.get_text("\n", strip=True) if doc.body else "")
    return title, best_text, published


def extract_wechat_article(html: str) -> tuple[str, str, date | None, str | None]:
    doc = soup(html)
    title_node = doc.select_one("#activity-name, meta[property='og:title']")
    title = clean_text(
        (title_node.get("content") if title_node and title_node.name == "meta" else None)
        or (title_node.get_text(" ", strip=True) if title_node else "")
    )
    content_node = doc.select_one("#js_content")
    content = clean_text(content_node.get_text("\n", strip=True) if content_node else "")

    account_node = doc.select_one("#js_name, .profile_nickname")
    account = clean_text(account_node.get_text(" ", strip=True) if account_node else "") or None
    if not account:
        match = re.search(r'var\s+nickname\s*=\s*htmlDecode\("([^"]+)"\)', html)
        account = clean_text(match.group(1)) if match else None

    published = None
    match = re.search(r"var\s+ct\s*=\s*['\"](\d+)['\"]", html)
    if match:
        from datetime import datetime

        published = datetime.fromtimestamp(int(match.group(1))).date()
    if not published:
        published = parse_date(html[:10000])
    return title, content, published, account


def extract_json_object(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.DOTALL)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise
