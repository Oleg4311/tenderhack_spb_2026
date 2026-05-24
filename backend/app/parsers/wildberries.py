import asyncio
import logging
from urllib.parse import quote_plus

from app.parsers.browser import fetch_rendered_html
from app.parsers.common import ProductItem, SourceResult, default_geo, merge_product_data, normalize_price
from app.parsers.extractors import extract_characteristics_from_json, extract_dom_cards, extract_embedded_json, find_products_in_json
from app.parsers.http_client import Fetcher, json_headers

logger = logging.getLogger(__name__)

REGION_DEST = {"москва": "-1257786", "санкт-петербург": "-1275499", "спб": "-1275499"}
SEARCH_ENDPOINTS = [
    "https://search.wb.ru/exactmatch/ru/common/v7/search",
    "https://search.wb.ru/exactmatch/ru/common/v5/search",
]


def _dest(region: str) -> str:
    return REGION_DEST.get((region or "").lower(), "-1257786")


def _basket(nm_id: int) -> int:
    vol = nm_id // 100000
    for i, threshold in enumerate([143, 287, 431, 719, 1007, 1061, 1115, 1169, 1313, 1601, 1655, 1919, 2045, 2189, 2405, 2621, 2837], 1):
        if vol <= threshold:
            return i
    return 18


def _image_urls(nm_id: int) -> list[str]:
    vol = nm_id // 100000
    part = nm_id // 1000
    basket = _basket(nm_id)
    return [
        f"https://basket-{basket:02d}.wbbasket.ru/vol{vol}/part{part}/{nm_id}/images/c516x688/{i}.webp"
        for i in range(1, 7)
    ]


def _from_kopecks(val) -> float:
    """WB API returns prices in kopecks — convert to rubles."""
    try:
        return float(str(val or 0).replace(" ", "").replace(",", ".")) / 100
    except Exception:
        return 0.0


def _price_from_product(p: dict) -> tuple[float, float]:
    price = old = 0.0
    for size in p.get("sizes") or []:
        pb = size.get("price") or {}
        price = normalize_price(_from_kopecks(pb.get("total") or pb.get("product")))
        old = normalize_price(_from_kopecks(pb.get("basic") or pb.get("old")))
        if price:
            break
    return (
        price or normalize_price(_from_kopecks(p.get("salePriceU") or p.get("priceU"))),
        old or normalize_price(_from_kopecks(p.get("priceU"))),
    )


class WildberriesParser:
    source = "wildberries"

    async def search(self, query: str, region: str = "Москва", limit: int = 10, category: str = "") -> SourceResult:
        search_url = f"https://www.wildberries.ru/catalog/0/search.aspx?search={quote_plus(query)}"
        logger.info("[source=wb] query=%r region=%s limit=%d", query, region, limit)

        api_params = {
            "ab_testing": "false", "appType": "1", "curr": "rub",
            "dest": _dest(region), "query": query,
            "resultset": "catalog", "sort": "popular", "spp": "30", "page": "1", "lang": "ru",
        }

        # Пробуем оба эндпоинта параллельно, без прокси и с прокси — 4 запроса одновременно
        async def _try(endpoint: str, use_proxy: bool) -> list[dict]:
            try:
                async with Fetcher(use_proxy=use_proxy) as f:
                    resp = await asyncio.wait_for(
                        f.get_json(
                            endpoint,
                            source=self.source,
                            headers=json_headers(source=self.source),
                            params=api_params,
                            retries=0,
                        ),
                        timeout=6,
                    )
                if resp.blocked or not resp.json_data:
                    return []
                data = resp.json_data if isinstance(resp.json_data, dict) else {}
                return (data.get("data") or {}).get("products") or []
            except Exception:
                return []

        items: list[ProductItem] = []
        seen_ids: set[str] = set()

        rendered = None
        try:
            rendered = await asyncio.wait_for(
                fetch_rendered_html(
                    search_url,
                    referer="https://www.wildberries.ru/",
                    warmup_url="https://www.wildberries.ru/",
                    wait_selectors=['[data-nm-id]', 'article', '.product-card', 'a[href*="/catalog/"]'],
                    scroll_steps=4,
                    use_proxy=False,
                    block_assets=False,
                ),
                timeout=38,
            )
        except Exception as exc:
            logger.info("[source=wb] browser_direct_failed=%s", type(exc).__name__)

        if rendered and rendered.product_payloads:
            for payload in rendered.product_payloads:
                products = []
                if isinstance(payload, dict):
                    data_node = payload.get("data")
                    products = (data_node.get("products") or []) if isinstance(data_node, dict) else []
                if not products:
                    products = find_products_in_json(payload)
                for raw in products:
                    if not isinstance(raw, dict):
                        continue
                    pid = str(raw.get("id") or raw.get("nmId") or "")
                    if pid and pid in seen_ids:
                        continue
                    item = self._from_search_product(raw, region, category)
                    if item:
                        if pid:
                            seen_ids.add(pid)
                        items.append(item)
                        if len(items) >= limit:
                            break
                if len(items) >= limit:
                    break

        if not items and rendered and rendered.html:
            for data in extract_embedded_json(rendered.html):
                for raw in find_products_in_json(data):
                    item = self._from_search_product(raw, region, category) if isinstance(raw, dict) else None
                    if item:
                        items.append(item)
                        if len(items) >= limit:
                            break
                if items:
                    break
            if not items:
                for card in extract_dom_cards(rendered.html, "https://www.wildberries.ru/")[:limit]:
                    if card.get("title") and (card.get("price") or card.get("url")):
                        items.append(ProductItem(
                            source=self.source, sourceType="marketplace", realSourceHost="wildberries.ru",
                            title=card["title"], price=card["price"], url=card["url"] or search_url,
                            mainImage=card["image"], images=[card["image"]] if card["image"] else [],
                            rating=card["rating"], brand=card["brand"], productId=card["product_id"],
                            category=category, region=region, geo=default_geo(region),
                        ))

        logger.info("[source=wb] browser_products=%d status=%s reason=%s", len(items), rendered.status if rendered else "none", rendered.errorReason if rendered else "")

        if not items:
            results = await asyncio.gather(
                _try(SEARCH_ENDPOINTS[0], False),
                _try(SEARCH_ENDPOINTS[1], False),
                _try(SEARCH_ENDPOINTS[0], True),
                _try(SEARCH_ENDPOINTS[1], True),
            )
            for products in results:
                for raw in products:
                    if not isinstance(raw, dict):
                        continue
                    pid = str(raw.get("id") or "")
                    if pid and pid in seen_ids:
                        continue
                    item = self._from_search_product(raw, region, category)
                    if item:
                        if pid:
                            seen_ids.add(pid)
                        items.append(item)
                        if len(items) >= limit:
                            break
                if len(items) >= limit:
                    break
            logger.info("[source=wb] api_products=%d", len(items))

        # Обогащение деталями первых 2 товаров
        if items:
            async with Fetcher(use_proxy=False) as fetcher:
                for idx, item in enumerate(items[: min(limit, 2)]):
                    if not item.productId:
                        continue
                    try:
                        detail = await asyncio.wait_for(
                            self._detail(fetcher, item.productId, item.url, region, category), timeout=4
                        )
                    except Exception:
                        detail = None
                    items[idx] = merge_product_data(item, detail)

        items = items[:limit]
        logger.info("[source=wb] relevant_products=%d", len([i for i in items if i.title and i.price]))

        if not items:
            logger.info("[source=wb] final_browser_status=%s final_browser_reason=%s", rendered.status if rendered else "none", rendered.errorReason if rendered else "")
            return SourceResult(
                self.source, "blocked",
                errorReason="WB API unreachable — IP blocked or network issue",
                diagnostics={"operatorAction": "WB requires residential proxy or Russian IP"},
            )
        return SourceResult(self.source, "ok", len(items), "", items)

    def _from_search_product(self, p: dict, region: str, category: str) -> ProductItem | None:
        nm_id = p.get("id")
        name = p.get("name")
        if not nm_id or not name:
            return None
        if not (p.get("sizes") or p.get("salePriceU") or p.get("priceU") or p.get("feedbacks") is not None):
            return None
        brand = p.get("brand") or ""
        price, old = _price_from_product(p)
        images = _image_urls(int(nm_id))
        chars = {
            "subject": p.get("subjectName") or "",
            "colors": ", ".join(c.get("name", "") for c in (p.get("colors") or []) if c.get("name")),
            "sizes": ", ".join(s.get("name", "") for s in (p.get("sizes") or []) if s.get("name")),
        }
        chars.update(extract_characteristics_from_json(p, limit=80))
        chars = {k: v for k, v in chars.items() if v}
        return ProductItem(
            source=self.source, sourceType="marketplace", realSourceHost="wildberries.ru",
            title=f"{brand} {name}".strip(), brand=brand,
            sku=str(nm_id), productId=str(nm_id),
            category=category or p.get("subjectName") or "",
            price=price, oldPrice=old,
            discountPercent=float(p.get("sale") or 0),
            seller=p.get("supplier") or p.get("supplierName") or "",
            rating=float(p.get("reviewRating") or 0),
            reviewsCount=int(p.get("feedbacks") or 0),
            images=images, mainImage=images[0] if images else "",
            url=f"https://www.wildberries.ru/catalog/{nm_id}/detail.aspx",
            characteristics=chars, region=region,
            geo=default_geo(region) | {"detectedRegion": region, "deliveryRegion": region},
        )

    async def _detail(self, fetcher: Fetcher, nm_id: str, url: str, region: str, category: str) -> ProductItem | None:
        detail_url = f"https://card.wb.ru/cards/v2/detail?appType=1&curr=rub&dest={_dest(region)}&spp=30&nm={nm_id}"
        resp = await fetcher.get_json(detail_url, source=self.source, referer="https://www.wildberries.ru/", retries=0)
        products = (((resp.json_data or {}).get("data") or {}).get("products") or [])
        card_meta = await self._card_metadata(fetcher, nm_id)
        if products:
            item = self._from_search_product(products[0], region, category)
            if item and card_meta:
                item.description = card_meta.get("description", "")
                item.characteristics.update(card_meta.get("characteristics", {}))
            return item
        return None

    async def _card_metadata(self, fetcher: Fetcher, nm_id: str) -> dict:
        try:
            nm_int = int(nm_id)
        except Exception:
            return {}
        vol = nm_int // 100000
        part = nm_int // 1000
        basket = _basket(nm_int)
        url = f"https://basket-{basket:02d}.wbbasket.ru/vol{vol}/part{part}/{nm_id}/info/ru/card.json"
        resp = await fetcher.get_json(url, source=self.source, referer="https://www.wildberries.ru/", retries=0)
        data = resp.json_data if isinstance(resp.json_data, dict) else {}
        if not data:
            return {}
        chars = extract_characteristics_from_json(data, limit=100)
        for group in data.get("grouped_options") or []:
            for option in group.get("options") or []:
                name = option.get("name")
                value = option.get("value")
                if name and value:
                    chars[str(name)] = str(value)
        return {"description": str(data.get("description") or ""), "characteristics": chars}
