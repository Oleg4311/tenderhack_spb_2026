import asyncio
import logging
from urllib.parse import quote_plus

from app.parsers.browser import fetch_rendered_html
from app.parsers.common import ProductItem, SourceResult, default_geo, merge_product_data, normalize_price
from app.parsers.extractors import (
    extract_characteristics_from_json,
    extract_dom_cards,
    extract_embedded_json,
    extract_product_from_html,
    extract_product_links,
    find_products_in_json,
)
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


def _price_from_product(p: dict) -> tuple[float, float]:
    price = old = 0.0
    for size in p.get("sizes") or []:
        pb = size.get("price") or {}
        price = normalize_price(pb.get("total") or pb.get("product") or pb.get("sale"))
        old = normalize_price(pb.get("basic") or pb.get("old"))
        if price:
            break
    return price or normalize_price(p.get("salePriceU") or p.get("priceU")), old or normalize_price(p.get("priceU"))


class WildberriesParser:
    source = "wildberries"

    async def search(self, query: str, region: str = "Москва", limit: int = 10, category: str = "") -> SourceResult:
        search_url = f"https://www.wildberries.ru/catalog/0/search.aspx?search={quote_plus(query)}"
        logger.info("[source=wb] query=%r region=%s limit=%d", query, region, limit)

        items: list[ProductItem] = []
        blocked_reason = ""
        rendered = None

        # ── 1. HTTP-first: both search.wb.ru endpoints concurrently (no proxy) ──
        # Running in parallel so max wait is 6s, not 6s×2.
        # WB public search API often allows direct connections.
        params = {
            "ab_testing": "false", "appType": "1", "curr": "rub",
            "dest": _dest(region), "query": query,
            "resultset": "catalog", "sort": "popular", "spp": "30", "page": "1", "lang": "ru",
        }

        async def _try_wb_api(endpoint: str, use_proxy: bool) -> list[dict]:
            try:
                async with Fetcher(use_proxy=use_proxy) as f:
                    resp = await asyncio.wait_for(
                        f.get_json(endpoint, source=self.source, headers=json_headers(source=self.source), params=params, retries=0),
                        timeout=6,
                    )
                if resp.blocked:
                    return []
                data = resp.json_data if isinstance(resp.json_data, dict) else {}
                return (data.get("data") or {}).get("products") or []
            except Exception:
                return []

        results = await asyncio.gather(
            _try_wb_api(SEARCH_ENDPOINTS[0], False),
            _try_wb_api(SEARCH_ENDPOINTS[1], False),
        )
        for products in results:
            for raw in products[:limit]:
                item = self._from_search_product(raw, region, category)
                if item:
                    items.append(item)
            if items:
                break

        if items:
            logger.info("[source=wb] http_direct_products=%d", len(items))
        else:
            blocked_reason = "WB API blocked (direct)"

        # ── 2. Browser fallback via Playwright (when HTTP is blocked) ─────────
        if not items:
            try:
                rendered = await asyncio.wait_for(
                    fetch_rendered_html(
                        search_url,
                        referer="https://www.wildberries.ru/",
                        warmup_url="https://www.wildberries.ru/",
                        wait_selectors=['[data-nm-id]', 'article', '.product-card', 'a[href*="/catalog/"]'],
                        scroll_steps=3,
                        use_proxy=True,
                    ),
                    timeout=33,
                )
            except asyncio.TimeoutError:
                rendered = None
                blocked_reason = blocked_reason or "browser timeout"
            except Exception as exc:
                rendered = None
                blocked_reason = blocked_reason or f"browser error: {type(exc).__name__}"

            logger.info(
                "[source=wb] page_loaded=%s xhr_payloads=%d xhr_product_payloads=%d status=%s",
                rendered.page_loaded if rendered else False,
                rendered.xhr_payloads if rendered else 0,
                rendered.xhr_product_payloads if rendered else 0,
                rendered.status if rendered else "none",
            )

            # XHR product payloads
            if rendered and rendered.product_payloads:
                seen_ids: set[str] = set()
                for payload in rendered.product_payloads:
                    if isinstance(payload, list):
                        wb_products = payload
                    elif isinstance(payload, dict):
                        data_node = payload.get("data")
                        wb_products = (data_node.get("products") or []) if isinstance(data_node, dict) else []
                        if not wb_products:
                            wb_products = find_products_in_json(payload)
                    else:
                        continue
                    for raw in wb_products:
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
                logger.info("[source=wb] json_products=%d (from XHR)", len(items))

            # Embedded JSON fallback
            if not items and rendered and rendered.html:
                for data in extract_embedded_json(rendered.html):
                    wb_products = find_products_in_json(data)
                    for raw in wb_products[:limit]:
                        if isinstance(raw, dict):
                            item = self._from_search_product(raw, region, category)
                            if item:
                                items.append(item)
                    if items:
                        break
                logger.info("[source=wb] embedded_products=%d", len(items))

            # DOM card fallback
            if not items and rendered and rendered.html:
                cards = extract_dom_cards(rendered.html, "https://www.wildberries.ru/")
                for card in cards[:limit]:
                    if card.get("title") and (card.get("price") or card.get("url")):
                        item = ProductItem(
                            source=self.source, sourceType="marketplace", realSourceHost="wildberries.ru",
                            title=card["title"], price=card["price"],
                            url=card["url"] or search_url,
                            mainImage=card["image"], images=[card["image"]] if card["image"] else [],
                            rating=card["rating"], brand=card["brand"],
                            productId=card["product_id"],
                            category=category, region=region, geo=default_geo(region),
                        )
                        items.append(item)
                logger.info("[source=wb] dom_cards=%d", len(items))

        # ── 4. HTML product links fallback ────────────────────────────────────
        if not items and rendered and rendered.html:
            links = extract_product_links(rendered.html, "https://www.wildberries.ru/")
            async with Fetcher(use_proxy=False) as fetcher:
                for link in links[:limit]:
                    try:
                        resp = await asyncio.wait_for(
                            fetcher.get_text(link, source=self.source, referer="https://www.wildberries.ru/", retries=0),
                            timeout=8,
                        )
                    except Exception:
                        continue
                    if resp.text and not resp.blocked:
                        product = extract_product_from_html(resp.text, link, self.source)
                        product.region = region
                        product.geo = default_geo(region)
                        product.category = category
                        items.append(product)

        normalized_products = len(items)
        logger.info("[source=wb] normalized_products=%d", normalized_products)

        # ── 5. Detail enrichment — only when HTTP path was used (budget ≤ ~14s) ──
        # Skip when browser was used to stay within the 45s source timeout.
        if items and rendered is None:
            async with Fetcher(use_proxy=False) as fetcher:
                for idx, item in enumerate(items[: min(limit, 2)]):
                    if not item.productId:
                        continue
                    try:
                        detail = await asyncio.wait_for(self._detail(fetcher, item.productId, item.url, region, category), timeout=4)
                    except Exception:
                        detail = None
                    items[idx] = merge_product_data(item, detail)

        items = items[:limit]
        relevant = len([i for i in items if i.title and i.price])
        logger.info("[source=wb] relevant_products=%d", relevant)

        if not items:
            reason = blocked_reason or (rendered.errorReason if rendered else "all endpoints blocked")
            return SourceResult(
                self.source, "blocked" if blocked_reason else "empty",
                errorReason=reason,
                diagnostics={
                    "operatorAction": "configure PROXY_URL / use residential proxy for Wildberries",
                    "triedEndpoints": SEARCH_ENDPOINTS,
                } if blocked_reason else {},
            )
        return SourceResult(self.source, "ok", len(items), "", items)

    def _from_search_product(self, p: dict, region: str, category: str) -> ProductItem | None:
        nm_id = p.get("id")
        name = p.get("name")
        if not nm_id or not name:
            return None
        # Real WB products have price/size data — skip navigation/filter items
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
