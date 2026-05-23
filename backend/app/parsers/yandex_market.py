import asyncio
import logging
import re
from urllib.parse import quote_plus

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
from app.parsers.http_client import Fetcher, browser_headers

logger = logging.getLogger(__name__)

REGION_IDS = {"москва": 213, "санкт-петербург": 2, "спб": 2, "новосибирск": 65, "екатеринбург": 54}


class YandexMarketParser:
    source = "yandex_market"

    async def search(self, query: str, region: str = "Москва", limit: int = 10, category: str = "") -> SourceResult:
        rid = REGION_IDS.get((region or "").lower(), 213)
        search_url = f"https://market.yandex.ru/search?text={quote_plus(query)}&lr={rid}"
        logger.info("[source=ym] query=%r region=%s limit=%d", query, region, limit)

        items: list[ProductItem] = []
        blocked_reason = ""
        candidate_html = ""

        # ── 1. Browser-first with proxy (YM blocks VPN IPs via HTTP, browser may pass) ──
        for _use_proxy in [True, False]:
            try:
                rendered = await asyncio.wait_for(
                    fetch_rendered_html(
                        search_url,
                        referer="https://market.yandex.ru/",
                        warmup_url="https://market.yandex.ru/",
                        wait_selectors=[
                            '[data-zone-name*="product" i]', '[data-zone-name="snippet"]',
                            'article', '[data-auto*="product" i]', 'a[href*="/product"]',
                        ],
                        scroll_steps=3,
                        use_proxy=_use_proxy,
                    ),
                    timeout=35,
                )
            except asyncio.TimeoutError:
                rendered = None
                blocked_reason = f"browser timeout (use_proxy={_use_proxy})"
                continue
            except Exception as exc:
                rendered = None
                blocked_reason = f"browser error: {type(exc).__name__}"
                continue

            logger.info(
                "[source=ym] use_proxy=%s page_loaded=%s xhr_payloads=%d xhr_product_payloads=%d status=%s",
                _use_proxy,
                rendered.page_loaded if rendered else False,
                rendered.xhr_payloads if rendered else 0,
                rendered.xhr_product_payloads if rendered else 0,
                rendered.status if rendered else "none",
            )

            if rendered and rendered.status != "blocked":
                candidate_html = rendered.html or ""
                break
            if rendered:
                blocked_reason = rendered.errorReason or f"blocked (use_proxy={_use_proxy})"

        # ── 2. XHR product payloads ───────────────────────────────────────────
        if rendered and rendered.product_payloads:
            for payload in rendered.product_payloads:
                items.extend(self._items_from_json(payload, region, category, limit - len(items)))
                if len(items) >= limit:
                    break
            logger.info("[source=ym] json_products=%d (from XHR)", len(items))

        # ── 3. Embedded JSON (__NEXT_DATA__ etc.) ─────────────────────────────
        embedded_products = 0
        if not items and candidate_html:
            for data in extract_embedded_json(candidate_html):
                items.extend(self._items_from_json(data, region, category, limit - len(items)))
                if len(items) >= limit:
                    break
            embedded_products = len(items)
            logger.info("[source=ym] embedded_products=%d", embedded_products)

        # ── 4. DOM card fallback ───────────────────────────────────────────────
        dom_cards = 0
        if not items and candidate_html:
            cards = extract_dom_cards(candidate_html, "https://market.yandex.ru/")
            dom_cards = len(cards)
            for card in cards[:limit]:
                if card.get("title") and (card.get("price") or card.get("url")):
                    url = card["url"] or ""
                    if url and not url.startswith("https://market.yandex.ru"):
                        url = normalize_url(url, "https://market.yandex.ru/")
                    item = ProductItem(
                        source=self.source, sourceType="marketplace", realSourceHost="market.yandex.ru",
                        title=card["title"], price=card["price"],
                        url=url or search_url,
                        mainImage=card["image"], images=[card["image"]] if card["image"] else [],
                        rating=card["rating"], brand=card["brand"],
                        productId=card["product_id"],
                        category=category, region=region,
                        geo=default_geo(region) | {"detectedRegion": region},
                    )
                    items.append(item)
            logger.info("[source=ym] dom_cards=%d", dom_cards)

        # ── 5. HTTP fallback ───────────────────────────────────────────────────
        if not items:
            logger.info("[source=ym] trying HTTP fallback")
            async with Fetcher() as fetcher:
                headers = browser_headers(source=self.source)
                headers["Cookie"] = f"_region_id={rid}; yandex_gid={rid};"
                for url in [search_url]:
                    try:
                        resp = await asyncio.wait_for(
                            fetcher.get_text(url, source=self.source, headers=headers, retries=0),
                            timeout=12,
                        )
                    except Exception as exc:
                        blocked_reason = blocked_reason or f"HTTP error: {type(exc).__name__}"
                        continue
                    if resp.text and not resp.blocked:
                        candidate_html = resp.text
                        for data in extract_embedded_json(candidate_html):
                            items.extend(self._items_from_json(data, region, category, limit - len(items)))
                        break
                    blocked_reason = blocked_reason or f"HTTP {resp.status_code}: YM anti-bot / VPN flag"

        # ── 6. Product links fallback ─────────────────────────────────────────
        if not items and candidate_html:
            links = extract_product_links(candidate_html, "https://market.yandex.ru/")
            async with Fetcher() as fetcher:
                for link in links[:limit]:
                    try:
                        detail = await asyncio.wait_for(
                            self._detail(fetcher, link, region, category), timeout=5
                        )
                        if detail and detail.title:
                            items.append(detail)
                    except Exception:
                        continue

        normalized_products = len(items)
        logger.info("[source=ym] normalized_products=%d", normalized_products)

        # ── 7. Detail enrichment ──────────────────────────────────────────────
        async with Fetcher() as fetcher:
            for idx, item in enumerate(items[: min(limit, 3)]):
                try:
                    detail = await asyncio.wait_for(self._detail(fetcher, item.url, region, category), timeout=5)
                except Exception:
                    detail = None
                items[idx] = merge_product_data(item, detail)

        items = self._dedupe(items)[:limit]
        relevant = len([i for i in items if i.title and i.price])
        logger.info("[source=ym] relevant_products=%d", relevant)

        if not items:
            reason = blocked_reason or (rendered.errorReason if rendered else "no data extracted")
            return SourceResult(
                self.source, "blocked" if blocked_reason else "empty",
                errorReason=reason,
                diagnostics={
                    "blockedUrl": search_url,
                    "legalFallbacksTried": ["browser_proxy", "browser_direct", "embedded_json", "dom_cards", "http"],
                    "operatorAction": "Yandex Market detects VPN — try residential proxy",
                } if blocked_reason else {},
            )
        return SourceResult(self.source, "ok", len(items), "", items)

    def _items_from_json(self, data, region: str, category: str, limit: int) -> list[ProductItem]:
        out: list[ProductItem] = []
        # Structured product search first
        raw_products = find_products_in_json(data)
        for node in raw_products:
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
                    source=self.source, sourceType="marketplace", realSourceHost="market.yandex.ru",
                    title=str(title)[:300],
                    brand=str(node.get("brand") or vendor.get("name") or ""),
                    productId=str(product_id or ""),
                    price=price,
                    images=[image] if image else [], mainImage=image or "",
                    url=normalize_url(url, "https://market.yandex.ru/"),
                    rating=float((node.get("ratings") or {}).get("value") if isinstance(node.get("ratings"), dict) else node.get("rating") or 0),
                    reviewsCount=int(node.get("reviewCount") or node.get("opinionsCount") or 0),
                    category=category, region=region,
                    geo=default_geo(region) | {"detectedRegion": region},
                    characteristics=extract_characteristics_from_json(node, limit=100),
                ))
            if len(out) >= limit:
                break
        if out:
            return self._dedupe(out)
        # Walk all nodes
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
                    source=self.source, sourceType="marketplace", realSourceHost="market.yandex.ru",
                    title=str(title)[:300],
                    brand=str(node.get("brand") or vendor.get("name") or ""),
                    productId=str(product_id or ""),
                    price=price,
                    images=[image] if image else [], mainImage=image or "",
                    url=normalize_url(url, "https://market.yandex.ru/"),
                    rating=float((node.get("ratings") or {}).get("value") if isinstance(node.get("ratings"), dict) else node.get("rating") or 0),
                    reviewsCount=int(node.get("reviewCount") or node.get("opinionsCount") or 0),
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
        resp = await fetcher.get_text(url, source=self.source, referer="https://market.yandex.ru/", retries=0)
        if resp.blocked or not resp.text:
            return None
        item = extract_product_from_html(resp.text, url, self.source)
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
