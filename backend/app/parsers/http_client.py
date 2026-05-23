import asyncio
import logging
import os
import random
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from app.parsers.common import detect_blocked_page

logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
]

REFERERS = {
    "wildberries": "https://www.wildberries.ru/",
    "ozon": "https://www.ozon.ru/",
    "yandex_market": "https://market.yandex.ru/",
    "runet": "https://www.google.com/",
}


@dataclass
class FetchResponse:
    url: str
    status_code: int = 0
    text: str = ""
    json_data: Any = None
    blocked: bool = False
    error: str = ""
    elapsed_ms: int = 0


class DomainRateLimiter:
    def __init__(self, min_delay: float = 0.7):
        self.min_delay = min_delay
        self._last: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def wait(self, domain: str) -> None:
        lock = self._locks.setdefault(domain, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            pause = self.min_delay - (now - self._last.get(domain, 0))
            if pause > 0:
                await asyncio.sleep(pause + random.uniform(0.05, 0.25))
            self._last[domain] = time.monotonic()


rate_limiter = DomainRateLimiter()


def _proxy_config() -> str | None:
    proxy_url = os.getenv("PROXY_URL")
    if proxy_url:
        return proxy_url
    proxy_list = [p.strip() for p in os.getenv("PROXY_LIST", "").split(",") if p.strip()]
    return random.choice(proxy_list) if proxy_list else None


def browser_headers(referer: str = "", source: str = "") -> dict[str, str]:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": referer or REFERERS.get(source, "https://www.google.com/"),
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-User": "?1",
        "Sec-CH-UA": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Platform": '"Windows"',
    }


def json_headers(referer: str = "", source: str = "") -> dict[str, str]:
    headers = browser_headers(referer, source)
    headers["Accept"] = "application/json, text/plain, */*"
    return headers


class Fetcher:
    def __init__(self):
        proxy = _proxy_config()
        http2_enabled = os.getenv("HTTPX_HTTP2", "1").lower() in {"1", "true", "yes"}
        if http2_enabled:
            try:
                import h2  # noqa: F401
            except ModuleNotFoundError:
                logger.warning("[HTTP] HTTP/2 requested but h2 is not installed; falling back to HTTP/1.1")
                http2_enabled = False
        kwargs: dict[str, Any] = {
            "timeout": httpx.Timeout(20.0, connect=5.0, read=12.0, write=5.0, pool=5.0),
            "follow_redirects": True,
            "headers": browser_headers(),
            "http2": http2_enabled,
        }
        if proxy:
            kwargs["proxy"] = proxy
        self.client = httpx.AsyncClient(**kwargs)

    async def get_text(
        self,
        url: str,
        *,
        source: str = "",
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        retries: int = 2,
        referer: str = "",
    ) -> FetchResponse:
        return await self._request("GET", url, source=source, headers=headers, params=params, retries=retries, referer=referer)

    async def get_json(
        self,
        url: str,
        *,
        source: str = "",
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        retries: int = 2,
        referer: str = "",
    ) -> FetchResponse:
        resp = await self._request("GET", url, source=source, headers=headers, params=params, retries=retries, referer=referer)
        if resp.text and resp.json_data is None:
            try:
                resp.json_data = httpx.Response(200, content=resp.text).json()
            except Exception:
                pass
        return resp

    async def _request(self, method: str, url: str, **kwargs: Any) -> FetchResponse:
        source = kwargs.get("source", "")
        headers = kwargs.get("headers") or browser_headers(kwargs.get("referer", ""), source)
        params = kwargs.get("params")
        retries = min(int(kwargs.get("retries", 2)), 2)
        domain = urlparse(url).netloc or "unknown"
        last = FetchResponse(url=url)

        for attempt in range(retries + 1):
            started = time.perf_counter()
            try:
                await rate_limiter.wait(domain)
                response = await self.client.request(method, url, headers=headers, params=params)
                text = response.text or ""
                # Some Russian sites claim UTF-8 but actually serve CP1251 (e.g. Bitrix CMS)
                if "�" in text[:8000]:
                    try:
                        text = response.content.decode("cp1251")
                    except Exception:
                        pass
                last = FetchResponse(
                    url=str(response.url),
                    status_code=response.status_code,
                    text=text,
                    blocked=detect_blocked_page(text[:80_000], response.status_code),
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                )
                ctype = response.headers.get("content-type", "")
                if "json" in ctype:
                    try:
                        last.json_data = response.json()
                    except Exception:
                        pass
                if response.status_code < 400 and not last.blocked:
                    return last
                if response.status_code not in {403, 408, 429, 500, 502, 503, 504}:
                    return last
                if attempt < retries:
                    await asyncio.sleep((2 ** attempt) + random.uniform(0.2, 0.8))
            except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError) as exc:
                last = FetchResponse(url=url, blocked=False, error=exc.__class__.__name__, elapsed_ms=int((time.perf_counter() - started) * 1000))
                if attempt < retries:
                    await asyncio.sleep((2 ** attempt) + random.uniform(0.2, 0.8))
            except Exception as exc:
                logger.info("[HTTP] request failed: %s %s", url, exc)
                last = FetchResponse(url=url, error=str(exc), elapsed_ms=int((time.perf_counter() - started) * 1000))
                break
        return last

    async def close(self) -> None:
        await self.client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.close()
