from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


DATE_PATTERNS = [
    re.compile(r"(?P<y>20\d{2})[年./-](?P<m>\d{1,2})[月./-](?P<d>\d{1,2})日?"),
    re.compile(r"(?P<y>20\d{2})(?P<m>\d{2})(?P<d>\d{2})"),
]


def clean_text(value: str) -> str:
    value = value.replace("\u3000", " ").replace("\xa0", " ")
    value = re.sub(r"[\t\r ]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def _wechat_captcha_target(parts) -> str | None:
    if parts.netloc.lower() != "mp.weixin.qq.com" or "appmsgcaptcha" not in parts.path:
        return None
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key == "target_url" and value.startswith("https://mp.weixin.qq.com/"):
            return value
    return None


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    captcha_target = _wechat_captcha_target(parts)
    if captcha_target:
        return normalize_url(captcha_target)
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in {"utm_source", "utm_medium", "utm_campaign", "spm", "from"}
    ]
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path, urlencode(query), "")
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(clean_text(value).encode("utf-8")).hexdigest()


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    for pattern in DATE_PATTERNS:
        match = pattern.search(value)
        if match:
            try:
                return date(int(match["y"]), int(match["m"]), int(match["d"]))
            except ValueError:
                continue
    return None


def iso_date(value: date | datetime | None) -> str | None:
    if value is None:
        return None
    return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()


def compact_name(value: str | None) -> str:
    if not value:
        return ""
    value = re.sub(r"[（(].*?[）)]", "", value)
    value = re.sub(r"有限公司|有限责任公司|股份|集团|公司|船舶|造船|重工", "", value)
    return re.sub(r"\W+", "", value).lower()
