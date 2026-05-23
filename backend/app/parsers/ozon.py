import asyncio
import re
from urllib.parse import quote_plus, urlparse

from app.parsers.browser import fetch_rendered_html
from app.parsers.common import ProductItem, SourceResult, default_geo, merge_product_data, normalize_price, normalize_url
from app.parsers.extractors import extract_characteristics_from_json, extract_embedded_json, extract_product_from_html, extract_product_links
from app.parsers.http_client import Fetcher, browser_headers


class OzonParser:
    source = "ozon"

    async def search(self, query: str, region: str = "Москва", limit: int = 10, category: str = "") -> SourceResult:
        search_url = f"https://www.ozon.ru/search/?text={quote_plus(query)}&from_global=true"
        search_urls = [
            search_url,
            f"https://www.ozon.ru/search/?from_global=true&text={quote_plus(query)}",
            f"https://www.ozon.ru/search/?deny_category_prediction=true&text={quote_plus(query)}",
        ]
        items: list[ProductItem] = []
        candidate_html = ""

        _conn_errors = 0
        _blocked_reason = ""
        async with Fetcher() as fetcher:
            composer = await self._composer_search(fetcher, query)
            if composer:
                items = self._items_from_json(composer, region, category, limit)

            resp = None
            if not items:
                for url in search_urls[:2]:
                    try:
                        resp = await asyncio.wait_for(
                            fetcher.get_text(url, source=self.source, headers=browser_headers(source=self.source), retries=0),
                            timeout=4,
                        )
                    except asyncio.TimeoutError:
                        _conn_errors += 1
                        _blocked_reason = _blocked_reason or "connection timeout — Ozon unreachable from current IP"
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
                        _blocked_reason = _blocked_reason or f"HTTP {resp.status_code}: Ozon anti-bot"

            if _conn_errors == 2 and not items:
                return SourceResult(
                    self.source,
                    "blocked",
                    errorReason=_blocked_reason,
                    diagnostics={"operatorAction": "configure PROXY_URL env variable to access Ozon"},
                )

            if not items and resp and resp.blocked:
                try:
                    rendered = await asyncio.wait_for(
                        fetch_rendered_html(
                            search_url,
                            referer="https://www.ozon.ru/",
                            wait_selectors=['a[href*="/product/"]', '[data-widget*="searchResults" i]'],
                            scroll_steps=2,
                        ),
                        timeout=15,
                    )
                except Exception:
                    rendered = None
                if not rendered:
                    return SourceResult(
                        self.source,
                        "blocked",
                        errorReason=_blocked_reason or "Ozon anti-bot — browser fallback failed",
                        diagnostics={"operatorAction": "configure PROXY_URL env variable to access Ozon"},
                    )
                if rendered.status == "blocked" and not rendered.product_payloads:
                    return SourceResult(
                        self.source,
                        "blocked",
                        errorReason="Ozon anti-bot or CAPTCHA",
                        diagnostics={
                            "blockedUrl": search_url,
                            "legalFallbacksTried": ["composer_json", "html", "playwright_render", "xhr_capture"],
                            "operatorAction": "configure PROXY_URL/PROXY_LIST env variable",
                        },
                    )
                candidate_html = rendered.html or candidate_html
                for payload in rendered.product_payloads or rendered.json_payloads:
                    items.extend(self._items_from_json(payload, region, category, limit - len(items)))
                if not items and candidate_html:
                    items = self._items_from_html(candidate_html, search_url, region, category, limit)
            elif not items and candidate_html:
                items = self._items_from_html(candidate_html, search_url, region, category, limit)

            if not items:
                links = extract_product_links(candidate_html, search_url)
                items = await self._details_from_links(fetcher, links, region, category, limit)
            else:
                for idx, item in enumerate(items[: min(limit, 3)]):
                    try:
                        detail = await asyncio.wait_for(self._detail(fetcher, item.url, region, category), timeout=3)
                    except Exception:
                        detail = None
                    items[idx] = merge_product_data(item, detail)

        items = self._dedupe(items)[:limit]
        return SourceResult(self.source, "ok" if items else "empty", len(items), "", items)

    async def _composer_search(self, fetcher: Fetcher, query: str) -> dict | list | None:
        encoded_path = quote_plus(f"/search/?text={query}&from_global=true")
        endpoints = [
            f"https://www.ozon.ru/api/composer-api.bx/page/json/v2?url={encoded_path}",
            f"https://www.ozon.ru/api/composer-api.bx/page/json/v2?url=/search/?text={quote_plus(query)}&from_global=true",
        ]
        headers = browser_headers("https://www.ozon.ru/", self.source)
        headers["Accept"] = "application/json, text/plain, */*"
        for endpoint in endpoints:
            try:
                resp = await asyncio.wait_for(
                    fetcher.get_json(endpoint, source=self.source, headers=headers, retries=0),
                    timeout=3,
                )
            except Exception:
                continue
            if resp.json_data and not resp.blocked:
                return resp.json_data
        return None

    def _items_from_html(self, html: str, base_url: str, region: str, category: str, limit: int) -> list[ProductItem]:
        items: list[ProductItem] = []
        for data in extract_embedded_json(html):
            items.extend(self._items_from_json(data, region, category, limit - len(items)))
            if len(items) >= limit:
                return self._dedupe(items)
        for link in extract_product_links(html, base_url)[:limit]:
            path_parts = [p for p in urlparse(link).path.split("/") if p]
            title = re.sub(r"[-_]+", " ", path_parts[-2] if len(path_parts) > 1 else path_parts[-1] if path_parts else "")
            items.append(ProductItem(source=self.source, realSourceHost="ozon.ru", title=title, url=link, region=region, category=category, geo=default_geo(region)))
        return self._dedupe(items)

    def _items_from_json(self, data, region: str, category: str, limit: int) -> list[ProductItem]:
        out: list[ProductItem] = []
        for node in self._walk(data):
            title = node.get("title") or node.get("name") or self._text(node)
            link = self._link(node)
            price = self._price(node)
            if title and link and (price or "/product/" in link):
                url = normalize_url(link, "https://www.ozon.ru/")
                image = self._image(node)
                out.append(ProductItem(
                    source=self.source,
                    sourceType="marketplace",
                    realSourceHost="ozon.ru",
                    title=str(title)[:300],
                    productId=str(node.get("id") or node.get("sku") or node.get("skuId") or ""),
                    price=price,
                    oldPrice=normalize_price(node.get("oldPrice") or node.get("originalPrice")),
                    images=[image] if image else [],
                    mainImage=image or "",
                    url=url,
                    brand=str(node.get("brand") or ""),
                    seller=str(node.get("seller") or ""),
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
        resp = await fetcher.get_text(url, source=self.source, referer="https://www.ozon.ru/", retries=0)
        if resp.blocked or not resp.text:
            try:
                rendered = await asyncio.wait_for(
                    fetch_rendered_html(
                        url,
                        referer="https://www.ozon.ru/",
                        wait_selectors=["h1", '[data-widget*="webProduct" i]', '[data-widget*="raShowcase" i]'],
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
        item.realSourceHost = "ozon.ru"
        item.region = region
        item.category = category
        item.geo = default_geo(region) | {k: v for k, v in item.geo.items() if v}
        return item

    def _walk(self, node, depth=0):
        if depth > 13:
            return
        if isinstance(node, dict):
            yield node
            for value in node.values():
                yield from self._walk(value, depth + 1)
        elif isinstance(node, list):
            for item in node[:350]:
                yield from self._walk(item, depth + 1)
        elif isinstance(node, str) and ("/product/" in node or "ozon.ru/product/" in node):
            yield {"url": node}

    def _link(self, node) -> str:
        for key in ("link", "url", "productUrl", "href"):
            value = node.get(key)
            if isinstance(value, str) and ("/product/" in value or "ozon.ru/product/" in value):
                return value
        action = node.get("action")
        if isinstance(action, dict):
            link = self._link(action)
            if link:
                return link
        for value in node.values():
            if isinstance(value, dict):
                link = self._link(value)
                if link:
                    return link
        return ""

    def _price(self, node) -> float:
        for key in ("price", "finalPrice", "cardPrice", "priceWithCard", "currentPrice"):
            price = normalize_price(node.get(key))
            if price:
                return price
        for value in node.values():
            if isinstance(value, dict):
                price = self._price(value)
                if price:
                    return price
        return 0

    def _image(self, node) -> str:
        for key in ("image", "imageUrl", "mainImage", "tileImage", "src", "coverImage"):
            value = node.get(key)
            if isinstance(value, str) and ("http" in value or value.startswith("//")):
                return normalize_url(value)
            if isinstance(value, dict):
                nested = self._image(value)
                if nested:
                    return nested
        return ""

    def _text(self, node) -> str:
        for value in node.values():
            if isinstance(value, dict) and isinstance(value.get("text"), str) and len(value["text"]) > 5:
                return value["text"]
        return ""

    def _dedupe(self, items: list[ProductItem]) -> list[ProductItem]:
        seen, out = set(), []
        for item in items:
            key = (item.url or item.title).split("?")[0]
            if key and key not in seen:
                seen.add(key)
                out.append(item)
        return out
