import asyncio
import re
from urllib.parse import quote_plus

from app.parsers.browser import fetch_rendered_html
from app.parsers.common import ProductItem, SourceResult, default_geo, merge_product_data, normalize_price, normalize_url
from app.parsers.extractors import extract_characteristics_from_json, extract_embedded_json, extract_product_from_html, extract_product_links
from app.parsers.http_client import Fetcher, browser_headers

REGION_IDS = {"москва": 213, "санкт-петербург": 2, "спб": 2, "новосибирск": 65, "екатеринбург": 54}


class YandexMarketParser:
    source = "yandex_market"

    async def search(self, query: str, region: str = "Москва", limit: int = 10, category: str = "") -> SourceResult:
        rid = REGION_IDS.get((region or "").lower(), 213)
        search_url = f"https://market.yandex.ru/search?text={quote_plus(query)}&lr={rid}"
        search_urls = [
            search_url,
            f"https://market.yandex.ru/search?cvredirect=0&text={quote_plus(query)}&lr={rid}",
            f"https://market.yandex.ru/search?hid=&text={quote_plus(query)}&lr={rid}",
        ]
        items: list[ProductItem] = []
        candidate_html = ""

        _conn_errors = 0
        _blocked_reason = ""
        async with Fetcher() as fetcher:
            headers = browser_headers(source=self.source)
            headers["Cookie"] = f"_region_id={rid}; yandex_gid={rid};"
            resp = None
            for url in search_urls[:2]:
                try:
                    resp = await asyncio.wait_for(
                        fetcher.get_text(url, source=self.source, headers=headers, retries=0),
                        timeout=4,
                    )
                except asyncio.TimeoutError:
                    _conn_errors += 1
                    _blocked_reason = _blocked_reason or "connection timeout — Yandex Market unreachable from current IP"
                    continue
                except Exception as exc:
                    _conn_errors += 1
                    _blocked_reason = _blocked_reason or f"connection error: {type(exc).__name__}"
                    continue
                if resp.text and not resp.blocked:
                    candidate_html = resp.text
                    search_url = url
                    break
                if resp.blocked:
                    _blocked_reason = _blocked_reason or f"HTTP {resp.status_code}: Yandex Market anti-bot / VPN flag"

            # If all HTTP attempts failed, still try browser fallback
            need_browser = (_conn_errors >= 2 and not items) or (resp and resp.blocked and not items)
            if need_browser:
                try:
                    rendered = await asyncio.wait_for(
                        fetch_rendered_html(
                            search_url,
                            referer="https://market.yandex.ru/",
                            warmup_url="https://market.yandex.ru/",
                            wait_selectors=['[data-zone-name*="product" i]', "article", 'a[href*="/product"]', '[class*="Product" i]'],
                            scroll_steps=2,
                        ),
                        timeout=25,
                    )
                except Exception:
                    rendered = None
                if not rendered:
                    return SourceResult(
                        self.source,
                        "blocked",
                        errorReason=_blocked_reason or "Yandex Market anti-bot — browser fallback failed",
                        diagnostics={"operatorAction": "configure PROXY_URL env variable"},
                    )
                if rendered.status == "blocked" and not rendered.product_payloads:
                    return SourceResult(
                        self.source,
                        "blocked",
                        errorReason="Yandex Market access restricted",
                        diagnostics={
                            "blockedUrl": search_url,
                            "legalFallbacksTried": ["html", "playwright_render", "xhr_capture"],
                            "operatorAction": "open source manually or configure PROXY_URL/PROXY_LIST",
                        },
                    )
                candidate_html = rendered.html or candidate_html
                for payload in rendered.product_payloads or rendered.json_payloads:
                    items.extend(self._items_from_json(payload, region, category, limit - len(items)))

            if not items and candidate_html:
                items = self._items_from_html(candidate_html, search_url, region, category, limit)

            if not items:
                items = await self._details_from_links(fetcher, extract_product_links(candidate_html, search_url), region, category, limit)
            else:
                for idx, item in enumerate(items[: min(limit, 3)]):
                    try:
                        detail = await asyncio.wait_for(self._detail(fetcher, item.url, region, category), timeout=3)
                    except Exception:
                        detail = None
                    items[idx] = merge_product_data(item, detail)

        items = self._dedupe(items)[:limit]
        return SourceResult(self.source, "ok" if items else "empty", len(items), "", items)

    def _items_from_html(self, html: str, base_url: str, region: str, category: str, limit: int) -> list[ProductItem]:
        items: list[ProductItem] = []
        for data in extract_embedded_json(html):
            items.extend(self._items_from_json(data, region, category, limit - len(items)))
            if len(items) >= limit:
                return self._dedupe(items)
        for link in extract_product_links(html, base_url)[:limit]:
            title = re.sub(r"[-_]+", " ", link.rstrip("/").split("/")[-1])[:160]
            items.append(ProductItem(source=self.source, realSourceHost="market.yandex.ru", title=title, url=link, region=region, category=category, geo=default_geo(region)))
        return self._dedupe(items)

    def _items_from_json(self, data, region: str, category: str, limit: int) -> list[ProductItem]:
        out: list[ProductItem] = []
        for node in self._walk(data):
            title = node.get("title") or node.get("name") or node.get("modelName")
            url = node.get("url") or node.get("productUrl") or node.get("navnodeUrl") or node.get("link")
            product_id = node.get("id") or node.get("modelId") or node.get("skuId") or node.get("wareId")
            if not url and product_id:
                url = f"https://market.yandex.ru/product/{product_id}"
            price = normalize_price(node.get("price") or node.get("priceValue") or node.get("value"))
            if title and url:
                image = self._image(node)
                vendor = node.get("vendor") if isinstance(node.get("vendor"), dict) else {}
                out.append(ProductItem(
                    source=self.source,
                    sourceType="marketplace",
                    realSourceHost="market.yandex.ru",
                    title=str(title)[:300],
                    brand=str(node.get("brand") or vendor.get("name") or ""),
                    productId=str(product_id or ""),
                    price=price,
                    images=[image] if image else [],
                    mainImage=image or "",
                    url=normalize_url(url, "https://market.yandex.ru/"),
                    rating=float((node.get("ratings") or {}).get("value") if isinstance(node.get("ratings"), dict) else node.get("rating") or 0),
                    reviewsCount=int(node.get("reviewCount") or node.get("opinionsCount") or 0),
                    category=category,
                    region=region,
                    geo=default_geo(region) | {"detectedRegion": region},
                    characteristics=extract_characteristics_from_json(node, limit=100),
                ))
            if len(out) >= limit:
                break
        return self._dedupe(out)

    async def _details_from_links(self, fetcher: Fetcher, links: list[str], region: str, category: str, limit: int) -> list[ProductItem]:
        items: list[ProductItem] = []
        for link in links[:limit]:
            detail = await self._detail(fetcher, link, region, category)
            if detail and detail.title:
                items.append(detail)
        return items

    async def _detail(self, fetcher: Fetcher, url: str, region: str, category: str) -> ProductItem | None:
        if not url:
            return None
        resp = await fetcher.get_text(url, source=self.source, referer="https://market.yandex.ru/", retries=0)
        if resp.blocked or not resp.text:
            try:
                rendered = await asyncio.wait_for(
                    fetch_rendered_html(
                        url,
                        referer="https://market.yandex.ru/",
                        wait_selectors=["h1", '[data-zone-name*="product" i]', "article"],
                        scroll_steps=1,
                    ),
                    timeout=6,
                )
            except Exception:
                return None
            if rendered.status == "blocked" and not rendered.product_payloads:
                return None
            if rendered.product_payloads:
                payload_items = []
                for payload in rendered.product_payloads:
                    payload_items.extend(self._items_from_json(payload, region, category, 1))
                if payload_items:
                    return payload_items[0]
            if not rendered.html:
                return None
            html = rendered.html
        else:
            html = resp.text
        item = extract_product_from_html(html, url, self.source)
        item.sourceType = "marketplace"
        item.realSourceHost = "market.yandex.ru"
        item.region = region
        item.category = category
        item.geo = default_geo(region) | {k: v for k, v in item.geo.items() if v}
        return item

    def _walk(self, node, depth=0):
        if depth > 13:
            return
        if isinstance(node, dict):
            if any(k in node for k in ("title", "name", "modelName")) and any(k in node for k in ("id", "modelId", "skuId", "url", "productUrl", "link")):
                yield node
            for value in node.values():
                yield from self._walk(value, depth + 1)
        elif isinstance(node, list):
            for item in node[:350]:
                yield from self._walk(item, depth + 1)

    def _image(self, node) -> str:
        for key in ("picture", "image", "imageUrl", "thumbnail", "src"):
            value = node.get(key)
            if isinstance(value, dict):
                value = value.get("url") or value.get("src")
            if isinstance(value, str):
                return normalize_url(value)
        return ""

    def _dedupe(self, items: list[ProductItem]) -> list[ProductItem]:
        seen, out = set(), []
        for item in items:
            key = (item.url or item.title).split("?")[0]
            if key and key not in seen:
                seen.add(key)
                out.append(item)
        return out
