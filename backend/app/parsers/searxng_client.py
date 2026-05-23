"""
SearxNG клиент — получение ссылок на товары через self-hosted поисковик.

SearxNG развёрнут локально в Docker, агрегирует результаты из
Google, Bing, DuckDuckGo, Yandex — без ограничений по IP.

Используется как fallback-источник ссылок, когда прямые запросы
к маркетплейсам блокируются (429/403).
"""

import asyncio
import logging
import os
from typing import Optional
from urllib.parse import quote_plus, urlparse

import httpx

logger = logging.getLogger(__name__)

SEARXNG_URL = os.getenv("SEARXNG_URL", "http://searxng:8080")


async def searxng_search(
    query: str,
    *,
    site_filter: str = "",
    limit: int = 15,
    timeout: float = 30.0,
) -> list[dict]:
    """
    Поиск через SearxNG. Возвращает список {url, title, content}.
    """
    search_query = f"site:{site_filter} {query}" if site_filter else query

    params = {
        "q": search_query,
        "format": "json",
        "language": "ru",
        "pageno": 1,
    }

    url = f"{SEARXNG_URL}/search"

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, params=params)
            if resp.status_code != 200:
                logger.warning(f"[SearxNG] HTTP {resp.status_code}")
                return []

            data = resp.json()
            results = data.get("results", [])[:limit]

            logger.info(f"[SearxNG] '{search_query}' -> {len(results)} results")
            return results
    except httpx.ConnectError:
        logger.warning("[SearxNG] Connection refused — is SearxNG container running?")
        return []
    except httpx.ReadTimeout:
        logger.warning(f"[SearxNG] Read timeout ({timeout}s) for query: {search_query}")
        return []
    except Exception as exc:
        logger.warning(f"[SearxNG] Error: {exc}")
        return []


async def get_product_urls(
    query: str,
    source: str,
    limit: int = 10,
) -> list[str]:
    """
    Получает URL карточек товаров через SearxNG для конкретного маркетплейса.
    """
    site_map = {
        "wildberries": "wildberries.ru",
        "ozon": "ozon.ru",
        "yandex_market": "market.yandex.ru",
    }

    site = site_map.get(source)
    if not site:
        return []

    results = await searxng_search(query, site_filter=site, limit=limit * 2)

    urls: list[str] = []
    seen: set[str] = set()

    for r in results:
        url = r.get("url", "")
        if not url:
            continue

        parsed = urlparse(url)
        path_lower = parsed.path.lower()

        is_product = False
        if source == "wildberries":
            is_product = bool(
                "/catalog/" in path_lower
                and any(c.isdigit() for c in path_lower.split("/catalog/")[-1][:10])
                and "/search" not in path_lower
            )
        elif source == "ozon":
            is_product = "/product/" in path_lower
        elif source == "yandex_market":
            is_product = "/product/" in path_lower or "/offer/" in path_lower

        if not is_product:
            continue

        clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        if clean_url in seen:
            continue
        seen.add(clean_url)
        urls.append(url)

        if len(urls) >= limit:
            break

    logger.info(f"[SearxNG] {source}: {len(urls)} product URLs from {len(results)} search results")
    return urls


async def is_available() -> bool:
    """Проверяет доступность SearxNG."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{SEARXNG_URL}/healthz")
            return resp.status_code == 200
    except Exception:
        return False
