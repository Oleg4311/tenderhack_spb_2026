"""
Патч для WildberriesParser — добавляет SearxNG fallback.

Когда search.wb.ru возвращает 429, получаем ссылки на товары
через SearxNG (Google/Yandex site:wildberries.ru) и парсим карточки.

Применение: заменить метод search() в wildberries.py или
добавить вызов _searxng_fallback() в конец существующего search().
"""

import asyncio
import logging
from urllib.parse import quote_plus

from app.parsers.common import ProductItem, SourceResult, default_geo, normalize_price
from app.parsers.extractors import (
    extract_characteristics_from_json,
    extract_product_from_html,
)
from app.parsers.http_client import Fetcher, json_headers
from app.parsers.searxng_client import get_product_urls, is_available

logger = logging.getLogger(__name__)


async def wb_searxng_fallback(
    query: str,
    region: str = "Москва",
    limit: int = 10,
    category: str = "",
) -> list[ProductItem]:
    """
    SearxNG fallback для WB: ищем ссылки через поисковики,
    парсим карточки через JSON API WB (card.wb.ru).
    """
    if not await is_available():
        logger.info("[WB/SearxNG] SearxNG not available")
        return []

    urls = await get_product_urls(query, "wildberries", limit=limit)
    if not urls:
        return []

    items: list[ProductItem] = []
    
    # Извлекаем nm_id из URL вида /catalog/12345678/detail.aspx
    nm_ids = []
    for url in urls:
        parts = url.rstrip("/").split("/")
        for part in parts:
            if part.isdigit() and len(part) >= 6:
                nm_ids.append(part)
                break

    if not nm_ids:
        return []

    # Запрашиваем данные через card.wb.ru (менее блокируемый endpoint)
    async with Fetcher() as fetcher:
        # Batch-запрос: до 100 товаров за раз
        batch = ";".join(nm_ids[:limit])
        dest = _dest_code(region)
        card_url = f"https://card.wb.ru/cards/v2/detail?appType=1&curr=rub&dest={dest}&spp=30&nm={batch}"
        
        resp = await fetcher.get_json(
            card_url,
            source="wildberries",
            headers=json_headers(referer="https://www.wildberries.ru/", source="wildberries"),
            retries=1,
        )
        
        if resp.json_data and not resp.blocked:
            products = ((resp.json_data or {}).get("data") or {}).get("products") or []
            for p in products[:limit]:
                item = _parse_wb_product(p, region, category)
                if item:
                    items.append(item)
            if items:
                logger.info(f"[WB/SearxNG] Got {len(items)} items via card.wb.ru batch")
                return items

        # Fallback: парсим каждую карточку отдельно через HTML
        for url in urls[:limit]:
            try:
                resp = await asyncio.wait_for(
                    fetcher.get_text(url, source="wildberries", referer="https://www.wildberries.ru/", retries=0),
                    timeout=4,
                )
                if resp.text and not resp.blocked:
                    item = extract_product_from_html(resp.text, url, "wildberries")
                    if item and item.title:
                        item.region = region
                        item.category = category
                        item.geo = default_geo(region)
                        items.append(item)
            except Exception:
                continue

    logger.info(f"[WB/SearxNG] Total: {len(items)} items")
    return items


def _dest_code(region: str) -> str:
    REGION_DEST = {
        "москва": "-1257786",
        "санкт-петербург": "-1275499",
        "спб": "-1275499",
    }
    return REGION_DEST.get((region or "").lower(), "-1257786")


def _parse_wb_product(p: dict, region: str, category: str) -> ProductItem | None:
    nm_id = p.get("id")
    name = p.get("name")
    if not nm_id or not name:
        return None

    brand = p.get("brand") or ""
    price = old_price = 0.0
    
    for size in p.get("sizes") or []:
        pb = size.get("price") or {}
        price = normalize_price(pb.get("total") or pb.get("product") or pb.get("sale"))
        old_price = normalize_price(pb.get("basic") or pb.get("old"))
        if price:
            break
    
    if not price:
        price = normalize_price(p.get("salePriceU") or p.get("priceU"))

    nm_int = int(nm_id)
    vol = nm_int // 100_000
    part = nm_int // 1_000
    
    # Basket calculation
    thresholds = [143, 287, 431, 719, 1007, 1061, 1115, 1169, 1313, 1601, 1655, 1919, 2045, 2189, 2405, 2621, 2837]
    basket = 18
    for i, t in enumerate(thresholds, 1):
        if vol <= t:
            basket = i
            break

    images = [
        f"https://basket-{basket:02d}.wbbasket.ru/vol{vol}/part{part}/{nm_int}/images/c516x688/{i}.webp"
        for i in range(1, 4)
    ]

    chars = extract_characteristics_from_json(p, limit=80)
    chars = {k: v for k, v in chars.items() if v}

    return ProductItem(
        source="wildberries",
        sourceType="marketplace",
        realSourceHost="wildberries.ru",
        title=f"{brand} {name}".strip(),
        brand=brand,
        sku=str(nm_int),
        productId=str(nm_int),
        category=category or p.get("subjectName") or "",
        price=price,
        oldPrice=old_price,
        seller=p.get("supplier") or p.get("supplierName") or "",
        rating=float(p.get("reviewRating") or 0),
        reviewsCount=int(p.get("feedbacks") or 0),
        images=images,
        mainImage=images[0] if images else "",
        url=f"https://www.wildberries.ru/catalog/{nm_int}/detail.aspx",
        characteristics=chars,
        region=region,
        geo=default_geo(region),
    )
