import asyncio
from urllib.parse import quote_plus

from app.parsers.browser import fetch_rendered_html
from app.parsers.common import ProductItem, SourceResult, default_geo, merge_product_data, normalize_price
from app.parsers.extractors import extract_characteristics_from_json, extract_product_from_html, extract_product_links
from app.parsers.http_client import Fetcher, json_headers

REGION_DEST = {"москва": "-1257786", "санкт-петербург": "-1275499", "спб": "-1275499"}
SEARCH_ENDPOINTS = [
    "https://search.wb.ru/exactmatch/ru/common/v7/search",
    "https://search.wb.ru/exactmatch/ru/common/v5/search",
    "https://search.wb.ru/exactmatch/ru/common/v4/search",
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
        items: list[ProductItem] = []
        params = {
            "ab_testing": "false", "appType": "1", "curr": "rub", "dest": _dest(region),
            "query": query, "resultset": "catalog", "sort": "popular", "spp": "30", "page": "1", "lang": "ru",
        }
        async with Fetcher() as fetcher:
            blocked_reason = ""
            _conn_errors = 0
            for endpoint in SEARCH_ENDPOINTS[:2]:
                try:
                    resp = await asyncio.wait_for(
                        fetcher.get_json(endpoint, source=self.source, headers=json_headers(source=self.source), params=params, retries=0),
                        timeout=4,
                    )
                except asyncio.TimeoutError:
                    _conn_errors += 1
                    blocked_reason = blocked_reason or "connection timeout on WB search API (search.wb.ru unreachable)"
                    continue
                except Exception as exc:
                    _conn_errors += 1
                    blocked_reason = blocked_reason or f"connection error: {type(exc).__name__} — WB unreachable from current IP"
                    continue
                if resp.blocked:
                    blocked_reason = f"HTTP {resp.status_code}: blocked by Wildberries"
                    continue
                products = ((resp.json_data or {}).get("data") or {}).get("products") or []
                for raw in products[:limit]:
                    item = self._from_search_product(raw, region, category)
                    if item:
                        items.append(item)
                if items:
                    break

            for idx, item in enumerate(items[: min(limit, 3)]):
                try:
                    detail = await asyncio.wait_for(self._detail(fetcher, item.productId, item.url, region, category), timeout=2.5)
                except Exception:
                    detail = None
                items[idx] = merge_product_data(item, detail)

        if not items:
            try:
                rendered = await asyncio.wait_for(
                    fetch_rendered_html(
                        f"https://www.wildberries.ru/catalog/0/search.aspx?search={quote_plus(query)}",
                        referer="https://www.wildberries.ru/",
                        scroll_steps=2,
                    ),
                    timeout=15,
                )
            except asyncio.TimeoutError:
                rendered = None
                blocked_reason = blocked_reason or "browser fallback timeout — WB unreachable"
            except Exception as exc:
                rendered = None
                blocked_reason = blocked_reason or f"browser fallback error: {type(exc).__name__}"
            if not rendered:
                status = "blocked" if blocked_reason else "empty"
                return SourceResult(
                    self.source,
                    status,
                    errorReason=blocked_reason,
                    diagnostics={
                        "operatorAction": "configure PROXY_URL env variable to access Wildberries",
                        "triedEndpoints": SEARCH_ENDPOINTS[:2],
                    } if blocked_reason else {},
                )
            if rendered.status == "blocked":
                return SourceResult(
                    self.source,
                    "blocked",
                    errorReason=rendered.errorReason or blocked_reason,
                    diagnostics={"operatorAction": "configure PROXY_URL env variable"},
                )
            links = extract_product_links(rendered.html, "https://www.wildberries.ru/")
            async with Fetcher() as fetcher:
                for link in links[:limit]:
                    resp = await fetcher.get_text(link, source=self.source, referer="https://www.wildberries.ru/", retries=0)
                    if resp.text:
                        product = extract_product_from_html(resp.text, link, self.source)
                        product.region = region
                        product.geo = default_geo(region)
                        product.category = category
                        items.append(product)
        status = "ok" if items else ("blocked" if blocked_reason else "empty")
        return SourceResult(self.source, status, len(items), blocked_reason if not items else "", items[:limit])

    def _from_search_product(self, p: dict, region: str, category: str) -> ProductItem | None:
        nm_id = p.get("id")
        name = p.get("name")
        if not nm_id or not name:
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
            source=self.source,
            sourceType="marketplace",
            realSourceHost="wildberries.ru",
            title=f"{brand} {name}".strip(),
            brand=brand,
            sku=str(nm_id),
            productId=str(nm_id),
            category=category or p.get("subjectName") or "",
            price=price,
            oldPrice=old,
            discountPercent=float(p.get("sale") or 0),
            seller=p.get("supplier") or p.get("supplierName") or "",
            rating=float(p.get("reviewRating") or 0),
            reviewsCount=int(p.get("feedbacks") or 0),
            images=images,
            mainImage=images[0] if images else "",
            url=f"https://www.wildberries.ru/catalog/{nm_id}/detail.aspx",
            characteristics=chars,
            region=region,
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
        html = await fetcher.get_text(url, source=self.source, referer="https://www.wildberries.ru/", retries=0)
        if html.text and not html.blocked:
            item = extract_product_from_html(html.text, url, self.source)
            if card_meta:
                item.description = item.description or card_meta.get("description", "")
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
