"""
Патч для YandexMarketParser — добавляет SearxNG fallback.

Когда ЯМ возвращает 403/SmartCaptcha, получаем ссылки через SearxNG
и парсим карточки товаров.
"""

import asyncio
import logging

from app.parsers.common import ProductItem, SourceResult, default_geo
from app.parsers.extractors import extract_product_from_html
from app.parsers.http_client import Fetcher, browser_headers
from app.parsers.browser import fetch_rendered_html
from app.parsers.searxng_client import get_product_urls, is_available

logger = logging.getLogger(__name__)


async def ym_searxng_fallback(
    query: str,
    region: str = "Москва",
    limit: int = 10,
    category: str = "",
) -> list[ProductItem]:
    """
    SearxNG fallback для ЯМ.
    """
    if not await is_available():
        logger.info("[YM/SearxNG] SearxNG not available")
        return []

    urls = await get_product_urls(query, "yandex_market", limit=limit)
    if not urls:
        return []

    items: list[ProductItem] = []

    # Стратегия 1: httpx
    async with Fetcher() as fetcher:
        for url in urls[:limit]:
            try:
                headers = browser_headers(referer="https://market.yandex.ru/", source="yandex_market")
                resp = await asyncio.wait_for(
                    fetcher.get_text(url, source="yandex_market", headers=headers, retries=0),
                    timeout=4,
                )
                if resp.text and not resp.blocked:
                    item = extract_product_from_html(resp.text, url, "yandex_market")
                    if item and item.title:
                        item.sourceType = "marketplace"
                        item.realSourceHost = "market.yandex.ru"
                        item.region = region
                        item.category = category
                        item.geo = default_geo(region)
                        items.append(item)
            except Exception:
                continue

    if items:
        logger.info(f"[YM/SearxNG] httpx: {len(items)} items from card pages")
        return items[:limit]

    # Стратегия 2: Playwright
    for url in urls[:min(limit, 5)]:
        try:
            rendered = await asyncio.wait_for(
                fetch_rendered_html(
                    url,
                    referer="https://market.yandex.ru/",
                    wait_selectors=["h1", '[data-zone-name*="product" i]', "article"],
                    scroll_steps=1,
                ),
                timeout=10,
            )
            if rendered.status == "ok" and rendered.html:
                item = extract_product_from_html(rendered.html, url, "yandex_market")
                if item and item.title:
                    item.sourceType = "marketplace"
                    item.realSourceHost = "market.yandex.ru"
                    item.region = region
                    item.category = category
                    item.geo = default_geo(region)
                    items.append(item)
        except Exception:
            continue

    logger.info(f"[YM/SearxNG] Playwright: {len(items)} items from card pages")
    return items[:limit]
