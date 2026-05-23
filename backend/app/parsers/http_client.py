import asyncio
import logging
import os
import random
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse, urlencode

try:
    from curl_cffi.requests import AsyncSession as CurlSession
    _HAS_CURL = True
except ImportError:
    _HAS_CURL = False

import httpx

from app.parsers.common import detect_blocked_page

logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

REFERERS = {
    "wildberries": "https://www.wildberries.ru/",
    "ozon": "https://www.ozon.ru/",
    "yandex_market": "https://market.yandex.ru/",
    "runet": "https://www.google.com/",
}

_IMPERSONATE_PROFILES = ["chrome124", "chrome120", "chrome110"]


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
    def __init__(self, min_delay: float = 0.5):
        self.min_delay = min_delay
        self._last: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def wait(self, domain: str) -> None:
        lock = self._locks.setdefault(domain, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            pause = self.min_delay - (now - self._last.get(domain, 0))
            if pause > 0:
                await asyncio.sleep(pause + random.uniform(0.05, 0.2))
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
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
    }


def json_headers(referer: str = "", source: str = "") -> dict[str, str]:
    headers = browser_headers(referer, source)
    headers["Accept"] = "application/json, text/plain, */*"
    headers["Sec-Fetch-Dest"] = "empty"
    headers["Sec-Fetch-Mode"] = "cors"
    return headers


def _decode_text(content: bytes, declared_text: str) -> str:
    text = declared_text or ""
    if "â" in text[:8000] or "Ã" in text[:8000]:
        try:
            return content.decode("cp1251")
        except Exception:
            pass
    return text


class Fetcher:
    def __init__(self):
        proxy = _proxy_config()
        self._proxy = proxy
        self._profile = random.choice(_IMPERSONATE_PROFILES)

        if _HAS_CURL:
            proxies = {"https": proxy, "http": proxy} if proxy else None
            self._curl: CurlSession = CurlSession(
                impersonate=self._profile,
                proxies=proxies,
                allow_redirects=True,
                max_redirects=5,
            )
        else:
            kwargs: dict[str, Any] = {
                "timeout": httpx.Timeout(20.0, connect=6.0, read=14.0),
                "follow_redirects": True,
                "headers": browser_headers(),
                "http2": False,
            }
            if proxy:
                kwargs["proxy"] = proxy
            self._httpx: httpx.AsyncClient = httpx.AsyncClient(**kwargs)

    async def get_text(self, url: str, *, source: str = "", headers: dict | None = None,
                       params: dict | None = None, retries: int = 2, referer: str = "") -> FetchResponse:
        return await self._request("GET", url, source=source, headers=headers,
                                   params=params, retries=retries, referer=referer)

    async def get_json(self, url: str, *, source: str = "", headers: dict | None = None,
                       params: dict | None = None, retries: int = 2, referer: str = "") -> FetchResponse:
        resp = await self._request("GET", url, source=source, headers=headers,
                                   params=params, retries=retries, referer=referer)
        if resp.text and resp.json_data is None:
            try:
                import json as _json
                resp.json_data = _json.loads(resp.text)
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

        if params:
            url = url + ("&" if "?" in url else "?") + urlencode(params)

        for attempt in range(retries + 1):
            started = time.perf_counter()
            try:
                await rate_limiter.wait(domain)

                if _HAS_CURL:
                    last = await self._curl_request(method, url, headers, started)
                else:
                    last = await self._httpx_request(method, url, headers, started)

                if last.status_code < 400 and not last.blocked:
                    return last
                if last.status_code not in {403, 408, 429, 500, 502, 503, 504}:
                    return last
                if attempt < retries:
                    await asyncio.sleep((2 ** attempt) + random.uniform(0.2, 0.8))

            except Exception as exc:
                logger.debug("[HTTP] %s %s attempt=%d error=%s", method, url, attempt, exc)
                last = FetchResponse(url=url, error=type(exc).__name__,
                                     elapsed_ms=int((time.perf_counter() - started) * 1000))
                if attempt < retries:
                    await asyncio.sleep((2 ** attempt) + random.uniform(0.2, 0.8))

        return last

    async def _curl_request(self, method: str, url: str, headers: dict, started: float) -> FetchResponse:
        response = await self._curl.request(method, url, headers=headers, timeout=20)
        text = _decode_text(response.content, response.text or "")
        blocked = detect_blocked_page(text[:80_000], response.status_code)
        resp = FetchResponse(
            url=str(response.url),
            status_code=response.status_code,
            text=text,
            blocked=blocked,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
        ctype = response.headers.get("content-type", "")
        if "json" in ctype or text.lstrip()[:1] in "{[":
            try:
                import json as _json
                resp.json_data = _json.loads(text)
            except Exception:
                pass
        return resp

    async def _httpx_request(self, method: str, url: str, headers: dict, started: float) -> FetchResponse:
        response = await self._httpx.request(method, url, headers=headers)
        text = _decode_text(response.content, response.text or "")
        blocked = detect_blocked_page(text[:80_000], response.status_code)
        resp = FetchResponse(
            url=str(response.url),
            status_code=response.status_code,
            text=text,
            blocked=blocked,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
        if "json" in response.headers.get("content-type", ""):
            try:
                resp.json_data = response.json()
            except Exception:
                pass
        return resp

    async def close(self) -> None:
        if _HAS_CURL:
            await self._curl.close()
        else:
            await self._httpx.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.close()
