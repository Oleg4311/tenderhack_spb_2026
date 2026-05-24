import asyncio
import logging
from urllib.parse import quote_plus

from app.parsers.browser import fetch_rendered_html, reset_context
from app.parsers.common import ProductItem, SourceResult, default_geo, merge_product_data, normalize_price, normalize_url
from app.parsers.extractors import (
    extract_characteristics_from_json,
    extract_dom_cards,
    extract_embedded_json,
    extract_product_from_html,
    find_products_in_json,
)
from app.parsers.http_client import Fetcher, browser_headers

logger = logging.getLogger(__name__)


class OzonParser:
    source = "ozon"

    async def search(self, query: str, region: str = "Москва", limit: int = 10, category: str = "") -> SourceResult:
        search_url = f"https://www.ozon.ru/search/?text={quote_plus(query)}&from_global=true"
        logger.info("[source=ozon] query=%r region=%s limit=%d", query, region, limit)

        items: list[ProductItem] = []
        blocked_reason = ""
        candidate_html = ""
        rendered = None

        # JS snippet evaluated inside browser after page loads — fetches Ozon composer API
        # using the session cookies already set by the browser, so it looks like a real XHR.
        _OZON_COMPOSER_JS = """
async () => {
    try {
        const params = new URLSearchParams(window.location.search);
        const text = params.get('text') || '';
        const apiUrl = 'https://api.ozon.ru/composer-api.bx/page/json/v2?url='
            + encodeURIComponent('/search/?text=' + encodeURIComponent(text) + '&from_global=true');
        const resp = await fetch(apiUrl, {
            method: 'GET',
            credentials: 'include',
            headers: {
                'Accept': 'application/json',
                'x-o3-app-name': 'ozonweb',
                'x-o3-app-version': '2.0',
                'x-o3-language': 'ru',
            },
        });
        if (!resp.ok) return null;
        return await resp.json();
    } catch(e) { return null; }
}
"""

        # ── 1. Playwright primary: proxy first, then direct ───────────────────
        # Persistent context хранит cookies между запросами (обход JS-challenge).
        # Scroll 4 раза с отслеживанием новых XHR — останавливаемся если нет прироста.
        for use_proxy in (True, False):
            try:
                rendered = await asyncio.wait_for(
                    fetch_rendered_html(
                        search_url,
                        referer="https://www.ozon.ru/",
                        warmup_url="https://www.ozon.ru/",
                        wait_selectors=[
                            'a[href*="/product/"]',
                            '[data-widget*="searchResults" i]',
                            'article',
                            '[class*="tile" i]',
                        ],
                        scroll_steps=4,
                        use_proxy=use_proxy,
                        after_load_evaluate=_OZON_COMPOSER_JS,
                    ),
                    timeout=42,
                )
            except asyncio.TimeoutError:
                rendered = None
                blocked_reason = blocked_reason or "browser timeout"
                break  # таймаут — не пробуем второй IP
            except Exception as exc:
                rendered = None
                blocked_reason = blocked_reason or f"browser error: {type(exc).__name__}"
                continue

            logger.info(
                "[source=ozon] proxy=%s page_loaded=%s xhr_payloads=%d xhr_product_payloads=%d status=%s",
                use_proxy,
                rendered.page_loaded if rendered else False,
                rendered.xhr_payloads if rendered else 0,
                rendered.xhr_product_payloads if rendered else 0,
                rendered.status if rendered else "none",
            )

            if rendered and rendered.status == "blocked":
                blocked_reason = blocked_reason or rendered.errorReason or "captcha/blocked"
                # Reset context explicitly for the other proxy mode too so both are clean
                asyncio.ensure_future(reset_context("www.ozon.ru", use_proxy=not use_proxy))
                # captcha/403 — не долбим второй IP, сразу переходим к HTTP fallback
                break

            # XHR: widgetStates / tileGrid / searchResults приходят через JSON
            if rendered and rendered.product_payloads:
                for payload in rendered.product_payloads:
                    items.extend(self._items_from_json(payload, region, category, limit - len(items)))
                    if len(items) >= limit:
                        break
                logger.info("[source=ozon] xhr_products=%d (proxy=%s)", len(items), use_proxy)

            # Embedded JSON из HTML страницы
            if not items and rendered and rendered.html:
                candidate_html = rendered.html
                for data in extract_embedded_json(rendered.html):
                    items.extend(self._items_from_json(data, region, category, limit - len(items)))
                    if len(items) >= limit:
                        break
                logger.info("[source=ozon] embedded_products=%d", len(items))

            if items:
                break  # нашли товары — не пробуем второй IP

        # ── 2. HTTP fallback (иногда Ozon отдаёт HTML без блокировки) ─────────
        if not items:
            async with Fetcher(use_proxy=False) as fetcher:
                try:
                    resp = await asyncio.wait_for(
                        fetcher.get_text(
                            search_url,
                            source=self.source,
                            headers=browser_headers(source=self.source),
                            retries=0,
                        ),
                        timeout=10,
                    )
                except Exception as exc:
                    blocked_reason = blocked_reason or f"HTTP error: {type(exc).__name__}"
                    resp = None
                if resp and resp.text and not resp.blocked:
                    candidate_html = resp.text
                elif resp:
                    blocked_reason = blocked_reason or f"HTTP {resp.status_code}: Ozon anti-bot"

        # Embedded JSON из HTTP HTML
        if not items and candidate_html:
            for data in extract_embedded_json(candidate_html):
                items.extend(self._items_from_json(data, region, category, limit - len(items)))
                if len(items) >= limit:
                    break
            if items:
                logger.info("[source=ozon] embedded_products_http=%d", len(items))

        # ── 3. DOM cards fallback ──────────────────────────────────────────────
        if not items and candidate_html:
            cards = extract_dom_cards(candidate_html, "https://www.ozon.ru/")
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
            logger.info("[source=ozon] dom_cards=%d", len(items))

        logger.info("[source=ozon] normalized_products=%d", len(items))

        # ── 4. Detail enrichment — только когда browser не использовался ──────
        if items and rendered is None:
            async with Fetcher(use_proxy=False) as fetcher:
                for idx, item in enumerate(items[: min(limit, 2)]):
                    try:
                        detail = await asyncio.wait_for(
                            self._detail(fetcher, item.url, region, category), timeout=4
                        )
                    except Exception:
                        detail = None
                    items[idx] = merge_product_data(item, detail)

        items = self._dedupe(items)[:limit]
        relevant = len([i for i in items if i.title and i.price])
        logger.info("[source=ozon] relevant_products=%d", relevant)

        if not items:
            reason = blocked_reason or (rendered.errorReason if rendered else "no data extracted")
            return SourceResult(
                self.source,
                "blocked" if blocked_reason else "empty",
                errorReason=reason,
                diagnostics={
                    "legalFallbacksTried": ["browser_proxy", "browser_direct", "http_direct", "embedded_json", "dom_cards"],
                    "operatorAction": "Ozon accessible from Russian IPs — check network connectivity or use residential proxy",
                } if blocked_reason else {},
            )
        return SourceResult(self.source, "ok", len(items), "", items)

    def _items_from_json(self, data, region: str, category: str, limit: int) -> list[ProductItem]:
        out: list[ProductItem] = []
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
