import asyncio
import json as _json
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
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
]

REFERERS = {
    "wildberries": "https://www.wildberries.ru/",
    "ozon": "https://www.ozon.ru/",
    "yandex_market": "https://market.yandex.ru/",
    "runet": "https://www.google.com/",
}

# Набор TLS-профилей: разные браузеры, разные версии — снижаем вероятность блокировки по JA3/JA4
_IMPERSONATE_PROFILES = [
    "chrome124", "chrome120", "chrome110", "chrome101",
    "firefox117",
    "safari15_5",
]

# Коды ответов, при которых прокси уходит в cooldown
_PROXY_BAN_CODES = {403, 407, 429, 451}
_PROXY_COOLDOWN_SEC = 600  # 10 минут


class ProxyManager:
    """Менеджер пула прокси с отслеживанием здоровья и cooldown'ом."""

    def __init__(self) -> None:
        self._proxies: list[str] = self._load_proxies()
        self._cooldown: dict[str, float] = {}
        self._failures: dict[str, int] = {}

    def _load_proxies(self) -> list[str]:
        result: list[str] = []
        url = os.getenv("PROXY_URL", "").strip()
        if url:
            result.append(url)
        for p in os.getenv("PROXY_LIST", "").split(","):
            p = p.strip()
            if p and p not in result:
                result.append(p)
        return result

    def get(self) -> str | None:
        if not self._proxies:
            return None
        now = time.monotonic()
        active = [p for p in self._proxies if self._cooldown.get(p, 0) < now]
        if active:
            return random.choice(active)
        # Все в cooldown — возвращаем тот, у кого cooldown заканчивается раньше
        return min(self._proxies, key=lambda p: self._cooldown.get(p, 0))

    def report_success(self, proxy: str | None) -> None:
        if proxy:
            self._failures.pop(proxy, None)
            self._cooldown.pop(proxy, None)

    def report_failure(self, proxy: str | None, status_code: int = 0) -> None:
        if not proxy:
            return
        self._failures[proxy] = self._failures.get(proxy, 0) + 1
        ban = status_code in _PROXY_BAN_CODES or self._failures[proxy] >= 3
        if ban:
            until = time.monotonic() + _PROXY_COOLDOWN_SEC
            self._cooldown[proxy] = until
            logger.info("[proxy] cooldown=%s status=%d fails=%d", proxy, status_code, self._failures[proxy])

    def has_proxies(self) -> bool:
        return bool(self._proxies)


proxy_manager = ProxyManager()


@dataclass
class FetchResponse:
    url: str
    status_code: int = 0
    text: str = ""
    json_data: Any = None
    blocked: bool = False
    error: str = ""
    elapsed_ms: int = 0
    proxy_used: str = ""


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
    """HTTP-клиент с curl_cffi (TLS-имперсонация Chrome/Firefox/Safari) + ротацией прокси."""

    def __init__(self):
        self._proxy = proxy_manager.get()
        # Каждый экземпляр получает случайный TLS-профиль — разные профили = разные JA3/JA4
        self._profile = random.choice(_IMPERSONATE_PROFILES)

        if _HAS_CURL:
            proxies = {"https": self._proxy, "http": self._proxy} if self._proxy else None
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
            if self._proxy:
                kwargs["proxy"] = self._proxy
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
        last = FetchResponse(url=url, proxy_used=self._proxy or "")

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

                last.proxy_used = self._proxy or ""

                if last.status_code < 400 and not last.blocked:
                    proxy_manager.report_success(self._proxy)
                    return last

                if last.status_code in _PROXY_BAN_CODES:
                    proxy_manager.report_failure(self._proxy, last.status_code)
                    # Пробуем сменить прокси на следующей попытке
                    new_proxy = proxy_manager.get()
                    if new_proxy and new_proxy != self._proxy:
                        self._proxy = new_proxy
                        await self._rebuild_client()

                if last.status_code not in {403, 408, 429, 500, 502, 503, 504}:
                    return last
                if attempt < retries:
                    await asyncio.sleep((2 ** attempt) + random.uniform(0.2, 0.8))

            except Exception as exc:
                logger.debug("[HTTP] %s %s attempt=%d error=%s", method, url, attempt, exc)
                last = FetchResponse(url=url, error=type(exc).__name__,
                                     elapsed_ms=int((time.perf_counter() - started) * 1000),
                                     proxy_used=self._proxy or "")
                if attempt < retries:
                    await asyncio.sleep((2 ** attempt) + random.uniform(0.2, 0.8))

        return last

    async def _rebuild_client(self) -> None:
        """Пересобирает curl_cffi сессию с новым прокси после ротации."""
        if not _HAS_CURL:
            return
        try:
            await self._curl.close()
        except Exception:
            pass
        proxies = {"https": self._proxy, "http": self._proxy} if self._proxy else None
        self._profile = random.choice(_IMPERSONATE_PROFILES)
        self._curl = CurlSession(
            impersonate=self._profile,
            proxies=proxies,
            allow_redirects=True,
            max_redirects=5,
        )

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
            try:
                await self._curl.close()
            except Exception:
                pass
        else:
            try:
                await self._httpx.aclose()
            except Exception:
                pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.close()
