import asyncio
import logging
import re
from urllib.parse import quote_plus, urlparse

from app.parsers.browser import fetch_rendered_html
from app.parsers.common import ProductItem, SourceResult, default_geo, merge_product_data, normalize_price, normalize_url
from app.parsers.extractors import (
    extract_characteristics_from_json,
    extract_dom_cards,
    extract_embedded_json,
    extract_product_from_html,
    extract_product_links,
    find_products_in_json,
)
from app.parsers.http_client import Fetcher, browser_headers, proxy_manager

logger = logging.getLogger(__name__)


class OzonParser:
    source = "ozon"

    async def search(self, query: str, region: str = "Москва", limit: int = 10, category: str = "") -> SourceResult:
        search_url = f"https://www.ozon.ru/search/?text={quote_plus(query)}&from_global=true"
        logger.info("[source=ozon] query=%r region=%s limit=%d", query, region, limit)

        items: list[ProductItem] = []
        blocked_reason = ""
        candidate_html = ""

        # ── 1. Browser-first WITHOUT proxy (Ozon blocks datacenter IPs, allows Russian ISPs) ──
        try:
            rendered = await asyncio.wait_for(
                fetch_rendered_html(
                    search_url,
                    referer="https://www.ozon.ru/",
                    warmup_url="https://www.ozon.ru/",
                    wait_selectors=['a[href*="/product/"]', '[data-widget*="searchResults" i]', 'article', '[class*="tile" i]'],
                    scroll_steps=3,
                    use_proxy=False,
                ),
                timeout=35,
            )
        except asyncio.TimeoutError:
            rendered = None
            blocked_reason = "browser timeout"
        except Exception as exc:
            rendered = None
            blocked_reason = f"browser error: {type(exc).__name__}"

        logger.info(
            "[source=ozon] page_loaded=%s xhr_payloads=%d xhr_product_payloads=%d status=%s",
            rendered.page_loaded if rendered else False,
            rendered.xhr_payloads if rendered else 0,
            rendered.xhr_product_payloads if rendered else 0,
            rendered.status if rendered else "none",
        )

        # ── 2. XHR product payloads ───────────────────────────────────────────
        if rendered and rendered.product_payloads:
            for payload in rendered.product_payloads:
                items.extend(self._items_from_json(payload, region, category, limit - len(items)))
                if len(items) >= limit:
                    break
            logger.info("[source=ozon] json_products=%d (from XHR)", len(items))

        # ── 3. Ozon composer API (structured JSON) ────────────────────────────
        if not items:
            async with Fetcher(use_proxy=False) as fetcher:
                composer = await self._composer_search(fetcher, query)
                logger.info("[source=ozon] composer_found=%s", composer is not None)
                if composer:
                    items = self._items_from_json(composer, region, category, limit)
                    logger.info("[source=ozon] composer_items=%d", len(items))

        # ── 4. Embedded JSON in rendered HTML ─────────────────────────────────
        embedded_products = 0
        if not items and rendered and rendered.html:
            candidate_html = rendered.html
            for data in extract_embedded_json(rendered.html):
                items.extend(self._items_from_json(data, region, category, limit - len(items)))
                if len(items) >= limit:
                    break
            embedded_products = len(items)
            logger.info("[source=ozon] embedded_products=%d", embedded_products)

        # ── 5. HTTP fallback (direct, no proxy) ───────────────────────────────
        if not items:
            async with Fetcher(use_proxy=False) as fetcher:
                for url in [search_url]:
                    try:
                        resp = await asyncio.wait_for(
                            fetcher.get_text(url, source=self.source, headers=browser_headers(source=self.source), retries=0),
                            timeout=12,
                        )
                    except Exception as exc:
                        blocked_reason = blocked_reason or f"HTTP error: {type(exc).__name__}"
                        continue
                    if resp.text and not resp.blocked:
                        candidate_html = resp.text
                        break
                    blocked_reason = blocked_reason or f"HTTP {resp.status_code}: Ozon anti-bot"

        # ── 6. Embedded JSON from HTTP HTML ───────────────────────────────────
        if not items and candidate_html:
            for data in extract_embedded_json(candidate_html):
                items.extend(self._items_from_json(data, region, category, limit - len(items)))
                if len(items) >= limit:
                    break
            if items:
                logger.info("[source=ozon] embedded_products_http=%d", len(items))

        # ── 7. DOM card fallback ───────────────────────────────────────────────
        dom_cards = 0
        if not items and candidate_html:
            cards = extract_dom_cards(candidate_html, "https://www.ozon.ru/")
            dom_cards = len(cards)
            for card in cards[:limit]:
                if card.get("title") and (card.get("price") or card.get("url")):
                    item = ProductItem(
                        source=self.source, sourceType="marketplace", realSourceHost="ozon.ru",
                        title=card["title"], price=card["price"],
                        url=card["url"] or search_url,
                        mainImage=card["image"], images=[card["image"]] if card["image"] else [],
                        rating=card["rating"], brand=card["brand"],
                        productId=card["product_id"],
                        category=category, region=region, geo=default_geo(region),
                    )
                    items.append(item)
            logger.info("[source=ozon] dom_cards=%d", dom_cards)

        normalized_products = len(items)
        logger.info("[source=ozon] normalized_products=%d", normalized_products)

        # ── 8. Detail enrichment ──────────────────────────────────────────────
        async with Fetcher(use_proxy=False) as fetcher:
            for idx, item in enumerate(items[: min(limit, 3)]):
                try:
                    detail = await asyncio.wait_for(self._detail(fetcher, item.url, region, category), timeout=6)
                except Exception:
                    detail = None
                items[idx] = merge_product_data(item, detail)

        items = self._dedupe(items)[:limit]
        relevant = len([i for i in items if i.title and i.price])
        logger.info("[source=ozon] relevant_products=%d", relevant)

        if not items:
            reason = blocked_reason or (rendered.errorReason if rendered else "no data extracted")
            return SourceResult(
                self.source, "blocked" if blocked_reason else "empty",
                errorReason=reason,
                diagnostics={
                    "legalFallbacksTried": ["browser", "composer_api", "embedded_json", "dom_cards"],
                    "operatorAction": "Ozon accessible from Russian IPs — check network connectivity",
                } if blocked_reason else {},
            )
        return SourceResult(self.source, "ok", len(items), "", items)

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
                    timeout=10,
                )
            except Exception:
                continue
            if resp.json_data and not resp.blocked:
                return resp.json_data
        return None

    def _items_from_json(self, data, region: str, category: str, limit: int) -> list[ProductItem]:
        out: list[ProductItem] = []
        # First try find_products_in_json for structured product objects
        raw_products = find_products_in_json(data)
        for node in raw_products:
            title = node.get("title") or node.get("name") or self._text(node)
            link = self._link(node)
            price = self._price(node)
            if title and link and (price or "/product/" in link):
                url = normalize_url(link, "https://www.ozon.ru/")
                image = self._image(node)
                out.append(ProductItem(
                    source=self.source, sourceType="marketplace", realSourceHost="ozon.ru",
                    title=str(title)[:300],
                    productId=str(node.get("id") or node.get("sku") or node.get("skuId") or ""),
                    price=price,
                    oldPrice=normalize_price(node.get("oldPrice") or node.get("originalPrice")),
                    images=[image] if image else [], mainImage=image or "",
                    url=url, brand=str(node.get("brand") or ""),
                    seller=str(node.get("seller") or ""),
                    category=category, region=region,
                    geo=default_geo(region) | {"detectedRegion": region},
                    characteristics=extract_characteristics_from_json(node, limit=100),
                ))
            if len(out) >= limit:
                break
        if out:
            return self._dedupe(out)
        # Fallback: walk all nodes
        for node in self._walk(data):
            title = node.get("title") or node.get("name") or self._text(node)
            link = self._link(node)
            price = self._price(node)
            if title and link and (price or "/product/" in link):
                url = normalize_url(link, "https://www.ozon.ru/")
                image = self._image(node)
                out.append(ProductItem(
                    source=self.source, sourceType="marketplace", realSourceHost="ozon.ru",
                    title=str(title)[:300],
                    productId=str(node.get("id") or node.get("sku") or node.get("skuId") or ""),
                    price=price,
                    oldPrice=normalize_price(node.get("oldPrice") or node.get("originalPrice")),
                    images=[image] if image else [], mainImage=image or "",
                    url=url, brand=str(node.get("brand") or ""),
                    seller=str(node.get("seller") or ""),
                    category=category, region=region,
                    geo=default_geo(region) | {"detectedRegion": region},
                    characteristics=extract_characteristics_from_json(node, limit=100),
                ))
            if len(out) >= limit:
                break
        return self._dedupe(out)

    async def _detail(self, fetcher: Fetcher, url: str, region: str, category: str) -> ProductItem | None:
        if not url:
            return None
        resp = await fetcher.get_text(url, source=self.source, referer="https://www.ozon.ru/", retries=0)
        if resp.blocked or not resp.text:
            return None
        item = extract_product_from_html(resp.text, url, self.source)
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
