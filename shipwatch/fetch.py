from __future__ import annotations

import time
from dataclasses import dataclass

import httpx


@dataclass(slots=True)
class FetchResult:
    url: str
    status_code: int
    text: str
    content_type: str


class Fetcher:
    def __init__(
        self,
        user_agent: str,
        timeout: int = 25,
        delay: float = 1.0,
    ):
        self.delay = delay
        self._last_request_at = 0.0
        self.client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": user_agent,
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
            },
        )

    def close(self) -> None:
        self.client.close()

    def get(self, url: str) -> FetchResult:
        wait = self.delay - (time.monotonic() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)
        response = self.client.get(url)
        self._last_request_at = time.monotonic()
        response.raise_for_status()
        if not response.encoding or response.encoding.lower() == "iso-8859-1":
            response.encoding = response.charset_encoding or response.apparent_encoding
        return FetchResult(
            url=str(response.url),
            status_code=response.status_code,
            text=response.text,
            content_type=response.headers.get("content-type", ""),
        )
