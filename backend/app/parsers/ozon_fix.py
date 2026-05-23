"""
Патч для OzonParser — добавляет SearxNG fallback.

Когда Ozon возвращает 403/challenge, получаем ссылки через SearxNG
и парсим отдельные карточки товаров (менее блокируемые).
"""

import asyncio
import logging

from app.parsers.common import ProductItem, SourceResult, default_geo
from app.parsers.extractors import extract_product_from_html
from app.parsers.http_client import Fetcher, browser_headers
from app.parsers.browser import fetch_rendered_html
from app.parsers.searxng_client import get_product_urls, is_available

logger = logging.getLogger(__name__)


async def ozon_searxng_fallback(
    query: str,
    region: str = "Москва",
    limit: int = 10,
    category: str = "",
) -> list[ProductItem]:
    """
    SearxNG fallback для Ozon: ищем ссылки через Google/Yandex,
    парсим карточки через Playwright (карточки блокируются реже поиска).
    """
    if not await is_available():
        logger.info("[Ozon/SearxNG] SearxNG not available")
        return []

    urls = await get_product_urls(query, "ozon", limit=limit)
    if not urls:
        return []

    items: list[ProductItem] = []

    # Стратегия 1: httpx (быстро, иногда работает для карточек)
    async with Fetcher() as fetcher:
        for url in urls[:limit]:
            try:
                resp = await asyncio.wait_for(
                    fetcher.get_text(
                        url,
                        source="ozon",
                        headers=browser_headers(referer="https://www.ozon.ru/", source="ozon"),
                        retries=0,
                    ),
                    timeout=4,
                )
                if resp.text and not resp.blocked:
                    item = extract_product_from_html(resp.text, url, "ozon")
                    if item and item.title and item.price:
                        item.sourceType = "marketplace"
                        item.realSourceHost = "ozon.ru"
                        item.region = region
                        item.category = category
                        item.geo = default_geo(region)
                        items.append(item)
            except Exception:
                continue

    if items:
        logger.info(f"[Ozon/SearxNG] httpx: {len(items)} items from card pages")
        return items[:limit]

    # Стратегия 2: Playwright для карточек (медленнее, но надёжнее)
    for url in urls[:min(limit, 5)]:  # Ограничиваем из-за медленности
        try:
            rendered = await asyncio.wait_for(
                fetch_rendered_html(
                    url,
                    referer="https://www.ozon.ru/",
                    wait_selectors=["h1", '[data-widget*="webProduct" i]'],
                    scroll_steps=1,
                ),
                timeout=10,
            )
            if rendered.status == "ok" and rendered.html:
                item = extract_product_from_html(rendered.html, url, "ozon")
                if item and item.title:
                    item.sourceType = "marketplace"
                    item.realSourceHost = "ozon.ru"
                    item.region = region
                    item.category = category
                    item.geo = default_geo(region)
                    items.append(item)
        except Exception:
            continue

    logger.info(f"[Ozon/SearxNG] Playwright: {len(items)} items from card pages")
    return items[:limit]
