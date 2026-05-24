import asyncio
import logging
from urllib.parse import quote_plus

from app.parsers.browser import fetch_rendered_html, reset_context
from app.parsers.common import ProductItem, SourceResult, default_geo, merge_product_data, normalize_price, normalize_url
from app.parsers.extractors import (
    extract_characteristics_from_json,
    extract_dom_cards,
    extract_embedded_json,
    extract_initial_state,
    extract_product_from_html,
    extract_ym_apiary_products,
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
        rendered = None

        # ── 1. HTTP direct (no proxy) — fast, YM often allows direct connections ──
        # Используем Google referer: market.yandex.ru referer триггерит VPN-блок.
        # Фиктивные Region-cookies также вызывают 403 — не добавляем их.
        async with Fetcher(use_proxy=False) as fetcher:
            headers = browser_headers(referer="https://www.google.com/")
            try:
                resp = await asyncio.wait_for(
                    fetcher.get_text(search_url, headers=headers, retries=0),
                    timeout=12,
                )
            except Exception as exc:
                blocked_reason = f"HTTP error: {type(exc).__name__}"
                resp = None
            if resp and resp.text and not resp.blocked:
                candidate_html = resp.text
                logger.info("[source=ym] http_direct status=%d len=%d", resp.status_code, len(resp.text))
                # Приоритет 1: Apiary noframes patches — самый надёжный источник данных на SSR странице
                apiary_items = self._items_from_apiary(extract_ym_apiary_products(candidate_html), region, category)
                items.extend(apiary_items[:limit])
                logger.info("[source=ym] http_apiary_products=%d", len(items))
                # Приоритет 2: __PRELOADED_STATE__ / initialState
                if not items:
                    for data in extract_initial_state(candidate_html):
                        items.extend(self._items_from_json(data, region, category, limit - len(items)))
                        if len(items) >= limit:
                            break
                # Приоритет 3: все остальные embedded JSON
                if not items:
                    for data in extract_embedded_json(candidate_html):
                        items.extend(self._items_from_json(data, region, category, limit - len(items)))
                        if len(items) >= limit:
                            break
                logger.info("[source=ym] http_direct_products=%d", len(items))
            elif resp:
                blocked_reason = f"HTTP {resp.status_code}: YM anti-bot / VPN flag"

        # ── 2. Browser: direct first (warmup yandex.ru для сессионных cookies) ─
        # Persistent context хранит Яндекс-cookies между запросами.
        # Scroll 4 раза с отслеживанием новых XHR.
        if not items:
            for use_proxy in (False, True):
                # Warmup yandex.ru даёт cross-domain cookies для market.yandex.ru
                warmup = "https://yandex.ru/" if not use_proxy else "https://market.yandex.ru/"
                try:
                    rendered = await asyncio.wait_for(
                        fetch_rendered_html(
                            search_url,
                            referer="https://market.yandex.ru/",
                            warmup_url=warmup,
                            wait_selectors=[
                                '[data-zone-name*="product" i]', '[data-zone-name="snippet"]',
                                'article', '[data-auto*="product" i]', 'a[href*="/product"]',
                            ],
                            scroll_steps=4,
                            use_proxy=use_proxy,
                        ),
                        timeout=32,
                    )
                except asyncio.TimeoutError:
                    rendered = None
                    blocked_reason = blocked_reason or "browser timeout"
                    break  # таймаут — не пробуем второй IP, в сумме превысим 45s
                except Exception as exc:
                    rendered = None
                    blocked_reason = blocked_reason or f"browser error: {type(exc).__name__}"
                    continue
                if rendered and rendered.status != "blocked":
                    break
                blocked_reason = blocked_reason or (rendered.errorReason if rendered else "blocked")
                asyncio.ensure_future(reset_context("market.yandex.ru", use_proxy=use_proxy))

            logger.info(
                "[source=ym] page_loaded=%s xhr_payloads=%d xhr_product_payloads=%d status=%s",
                rendered.page_loaded if rendered else False,
                rendered.xhr_payloads if rendered else 0,
                rendered.xhr_product_payloads if rendered else 0,
                rendered.status if rendered else "none",
            )

            if rendered and rendered.status != "blocked":
                candidate_html = rendered.html or candidate_html

            # XHR product payloads
            if rendered and rendered.product_payloads:
                for payload in rendered.product_payloads:
                    items.extend(self._items_from_json(payload, region, category, limit - len(items)))
                    if len(items) >= limit:
                        break
                logger.info("[source=ym] json_products=%d (from XHR)", len(items))

            # Apiary patches из browser HTML
            if not items and candidate_html:
                apiary_items = self._items_from_apiary(extract_ym_apiary_products(candidate_html), region, category)
                items.extend(apiary_items[:limit])
                logger.info("[source=ym] browser_apiary_products=%d", len(items))

            # __PRELOADED_STATE__ / initialState / __NEXT_DATA__ (YM хранит данные в этих переменных)
            if not items and candidate_html:
                for data in extract_initial_state(candidate_html):
                    items.extend(self._items_from_json(data, region, category, limit - len(items)))
                    if len(items) >= limit:
                        break
                logger.info("[source=ym] initial_state_products=%d", len(items))

            # Все остальные embedded JSON в <script> тегах
            if not items and candidate_html:
                for data in extract_embedded_json(candidate_html):
                    items.extend(self._items_from_json(data, region, category, limit - len(items)))
                    if len(items) >= limit:
                        break
                logger.info("[source=ym] embedded_products=%d", len(items))

            if rendered and rendered.status == "blocked":
                blocked_reason = blocked_reason or rendered.errorReason or "blocked (browser)"

        # ── 3. DOM card fallback ───────────────────────────────────────────────
        if not items and candidate_html:
            cards = extract_dom_cards(candidate_html, "https://market.yandex.ru/")
            for card in cards[:limit]:
                if card.get("title") and (card.get("price") or card.get("url")):
                    url = normalize_url(card["url"] or "", "https://market.yandex.ru/")
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
            logger.info("[source=ym] dom_cards=%d", len(items))

        # ── 4. Product links fallback ─────────────────────────────────────────
        if not items and candidate_html:
            from app.parsers.extractors import extract_product_links
            links = extract_product_links(candidate_html, "https://market.yandex.ru/")
            async with Fetcher(use_proxy=False) as fetcher:
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

        # ── 5. Detail enrichment — only when fast HTTP path was used ─────────
        if items and rendered is None:
            async with Fetcher(use_proxy=False) as fetcher:
                for idx, item in enumerate(items[: min(limit, 2)]):
                    try:
                        detail = await asyncio.wait_for(self._detail(fetcher, item.url, region, category), timeout=4)
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
                    "legalFallbacksTried": ["http_direct", "browser_direct", "embedded_json", "dom_cards"],
                    "operatorAction": "Yandex Market detects VPN/datacenter — try residential proxy",
                } if blocked_reason else {},
            )
        return SourceResult(self.source, "ok", len(items), "", items)

    def _items_from_apiary(self, apiary_products: list[dict], region: str, category: str) -> list[ProductItem]:
        out: list[ProductItem] = []
        for p in apiary_products:
            title = p.get("title", "")
            if not title:
                continue
            price = normalize_price(p.get("price"))
            url = p.get("url") or ""
            product_id = p.get("productId") or p.get("skuId") or ""
            image = normalize_url(p.get("picture") or "") if p.get("picture") else ""
            out.append(ProductItem(
                source=self.source, sourceType="marketplace", realSourceHost="market.yandex.ru",
                title=str(title)[:300],
                brand=str(p.get("brand") or ""),
                productId=str(product_id),
                price=price,
                images=[image] if image else [], mainImage=image,
                url=normalize_url(url, "https://market.yandex.ru/") if url else "",
                category=category, region=region,
                geo=default_geo(region) | {"detectedRegion": region},
            ))
        return self._dedupe(out)

    def _items_from_json(self, data, region: str, category: str, limit: int) -> list[ProductItem]:
        out: list[ProductItem] = []
        raw_products = find_products_in_json(data)
        for node in raw_products:
            title = node.get("title") or node.get("name") or node.get("modelName")
            url = node.get("url") or node.get("productUrl") or node.get("navnodeUrl") or node.get("link")
            product_id = node.get("id") or node.get("modelId") or node.get("skuId") or node.get("wareId")
            if not url and product_id:
                url = f"https://market.yandex.ru/product/{product_id}"
            price = self._extract_price(node)
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
                    rating=self._extract_rating(node),
                    reviewsCount=int(node.get("reviewCount") or node.get("opinionsCount") or 0),
                    category=category, region=region,
                    geo=default_geo(region) | {"detectedRegion": region},
                    characteristics=extract_characteristics_from_json(node, limit=100),
                ))
            if len(out) >= limit:
                break
        if out:
            return self._dedupe(out)
        # Walk all dict nodes as fallback
        for node in self._walk(data):
            title = node.get("title") or node.get("name") or node.get("modelName")
            url = node.get("url") or node.get("productUrl") or node.get("navnodeUrl") or node.get("link")
            product_id = node.get("id") or node.get("modelId") or node.get("skuId") or node.get("wareId")
            if not url and product_id:
                url = f"https://market.yandex.ru/product/{product_id}"
            price = self._extract_price(node)
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
                    rating=self._extract_rating(node),
                    reviewsCount=int(node.get("reviewCount") or node.get("opinionsCount") or 0),
                    category=category, region=region,
                    geo=default_geo(region) | {"detectedRegion": region},
                    characteristics=extract_characteristics_from_json(node, limit=100),
                ))
            if len(out) >= limit:
                break
        return self._dedupe(out)

    def _extract_price(self, node: dict) -> float:
        # Direct keys
        direct = normalize_price(node.get("price") or node.get("priceValue") or node.get("value"))
        if direct:
            return direct
        # Nested: prices.min.value, price.value, etc.
        for prices_key in ("prices", "price"):
            prices = node.get(prices_key)
            if isinstance(prices, dict):
                for sub_key in ("min", "current", "value"):
                    sub = prices.get(sub_key)
                    if isinstance(sub, dict):
                        v = normalize_price(sub.get("value") or sub.get("amount"))
                        if v:
                            return v
                    elif sub:
                        v = normalize_price(sub)
                        if v:
                            return v
        return 0.0

    def _extract_rating(self, node: dict) -> float:
        ratings = node.get("ratings")
        if isinstance(ratings, dict):
            v = ratings.get("value") or ratings.get("overall")
            if v:
                return float(v)
        return float(node.get("rating") or 0)

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
