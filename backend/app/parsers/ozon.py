import asyncio
import logging
from urllib.parse import quote_plus

from app.parsers.browser import fetch_rendered_html
from app.parsers.common import ProductItem, SourceResult, default_geo, normalize_price, normalize_url
from app.parsers.extractors import (
    extract_characteristics_from_json,
    extract_dom_cards,
    extract_embedded_json,
    find_products_in_json,
)

logger = logging.getLogger(__name__)

try:
    from curl_cffi.requests import AsyncSession as _CurlSession
    _HAS_CURL = True
except ImportError:
    _HAS_CURL = False

# Все известные внутренние эндпоинты Ozon (web BFF + мобильный)
_OZON_ENDPOINTS = [
    "https://api.ozon.ru/composer-api.bx/page/json/v2",
    "https://www.ozon.ru/api/composer-api.bx/page/json/v2",
    "https://api.ozon.ru/entrypoint-api.bx/page/json/v2",
]

# Android UA — другой маршрут через Kasada (mobile-клиенты обрабатываются иначе)
_ANDROID_UA = "ozonapp_android/17.16.0 (4.4; 1080x1920; ru; 30; XIAOMI Redmi Note 8 Pro; com.ozon.android)"
_IOS_UA = "ozonapp_ios/17.15.0 CFNetwork/1492.0.1 Darwin/23.3.0"

# TLS-профили для ротации JA3/JA4
_PROFILES = ["chrome124", "chrome120", "chrome116", "safari17_0", "edge101"]


class OzonParser:
    source = "ozon"

    async def search(self, query: str, region: str = "Москва", limit: int = 10, category: str = "") -> SourceResult:
        logger.info("[source=ozon] query=%r region=%s limit=%d", query, region, limit)

        search_path = f"/search/?text={quote_plus(query)}&from_global=true"
        search_url = f"https://www.ozon.ru/search/?text={quote_plus(query)}&from_global=true"

        items: list[ProductItem] = []
        blocked_reason = ""

        rendered = None
        for use_proxy, timeout in ((True, 24), (False, 10)):
            try:
                rendered = await asyncio.wait_for(
                    fetch_rendered_html(
                        search_url,
                        referer="https://www.ozon.ru/",
                        warmup_url="",
                        wait_selectors=['a[href*="/product/"]', '[data-widget*="searchResults" i]', 'article', '[class*="tile" i]'],
                        scroll_steps=1,
                        use_proxy=use_proxy,
                        block_assets=True,
                    ),
                    timeout=timeout,
                )
            except Exception as exc:
                logger.info("[source=ozon] browser_%s_failed=%s", "proxy" if use_proxy else "direct", type(exc).__name__)
                rendered = None
            if rendered and rendered.status != "blocked":
                break

        if rendered and rendered.product_payloads:
            for payload in rendered.product_payloads:
                items.extend(self._items_from_json(payload, region, category, limit - len(items)))
                if len(items) >= limit:
                    break

        if not items and rendered and rendered.html:
            for data in extract_embedded_json(rendered.html):
                items.extend(self._items_from_json(data, region, category, limit - len(items)))
                if len(items) >= limit:
                    break
            if not items:
                for card in extract_dom_cards(rendered.html, "https://www.ozon.ru/")[:limit]:
                    if card.get("title") and (card.get("price") or card.get("url")):
                        items.append(ProductItem(
                            source=self.source, sourceType="marketplace", realSourceHost="ozon.ru",
                            title=card["title"], price=card["price"], url=card["url"] or search_url,
                            mainImage=card["image"], images=[card["image"]] if card["image"] else [],
                            rating=card["rating"], brand=card["brand"], productId=card["product_id"],
                            category=category, region=region, geo=default_geo(region),
                        ))
        logger.info("[source=ozon] browser_products=%d status=%s reason=%s", len(items), rendered.status if rendered else "none", rendered.errorReason if rendered else "")

        if not items and not _HAS_CURL:
            blocked_reason = "browser did not extract products; curl_cffi not installed for HTTP fallback"

        # ── 1. Параллельный обстрел: web + мобильные заголовки × 3 эндпоинта ──
        # curl_cffi имитирует TLS Chrome/Safari на уровне JA3/JA4,
        # мобильные заголовки идут по другому пути внутри Kasada.
        tasks = []
        if not items and _HAS_CURL:
            for endpoint in _OZON_ENDPOINTS:
                url = f"{endpoint}?url={quote_plus(search_path)}"
                tasks.append(self._try_endpoint(url, _ANDROID_UA, "ozonapp_android", "chrome124", region, category, limit))
                tasks.append(self._try_endpoint(url, _IOS_UA, "ozonapp_ios", "safari17_0", region, category, limit))

        # Также пробуем web-заголовки с разными TLS-профилями
        web_url = f"{_OZON_ENDPOINTS[0]}?url={quote_plus(search_path)}"
        if not items and _HAS_CURL:
            for profile in ("chrome124", "chrome120", "edge101"):
                tasks.append(self._try_endpoint(web_url, None, "ozonweb", profile, region, category, limit))

        results = await asyncio.gather(*tasks, return_exceptions=True) if tasks else []

        for result in results:
            if isinstance(result, list) and result:
                items = result[:limit]
                logger.info("[source=ozon] api_products=%d", len(items))
                break

        # ── 2. Embedded JSON из прямого HTML через curl_cffi ──────────────────
        if not items and _HAS_CURL:
            html = await self._fetch_html(search_url)
            if html:
                for data in extract_embedded_json(html):
                    items.extend(self._items_from_json(data, region, category, limit - len(items)))
                    if len(items) >= limit:
                        break
                if items:
                    logger.info("[source=ozon] embedded_json=%d", len(items))

            # ── 3. DOM-карточки как последний fallback ────────────────────────
            if not items and html:
                cards = extract_dom_cards(html, "https://www.ozon.ru/")
                for card in cards[:limit]:
                    if card.get("title") and (card.get("price") or card.get("url")):
                        items.append(ProductItem(
                            source=self.source, sourceType="marketplace", realSourceHost="ozon.ru",
                            title=card["title"], price=card["price"],
                            url=card["url"] or search_url,
                            mainImage=card["image"], images=[card["image"]] if card["image"] else [],
                            rating=card["rating"], brand=card["brand"],
                            productId=card["product_id"],
                            category=category, region=region, geo=default_geo(region),
                        ))
                if items:
                    logger.info("[source=ozon] dom_cards=%d", len(items))

            if not html:
                blocked_reason = "Ozon: все эндпоинты вернули ошибку или блокировку"

        items = self._dedupe(items)[:limit]
        logger.info("[source=ozon] relevant_products=%d", len([i for i in items if i.title and i.price]))

        if not items:
            logger.info("[source=ozon] final_browser_status=%s final_browser_reason=%s", rendered.status if rendered else "none", rendered.errorReason if rendered else "")
            return SourceResult(
                self.source,
                "blocked" if blocked_reason else "empty",
                errorReason=blocked_reason or "Ozon: данные не получены",
                diagnostics={
                    "triedEndpoints": _OZON_ENDPOINTS,
                    "operatorAction": "Ozon Kasada — residential proxy or Russian IP needed",
                },
            )
        return SourceResult(self.source, "ok", len(items), "", items)

    async def _try_endpoint(
        self,
        url: str,
        user_agent: str | None,
        app_name: str,
        profile: str,
        region: str,
        category: str,
        limit: int,
    ) -> list[ProductItem]:
        """Один запрос к Ozon BFF с указанными заголовками и TLS-профилем."""
        try:
            headers: dict = {
                "Accept": "application/json",
                "Accept-Language": "ru-RU,ru;q=0.9",
                "Accept-Encoding": "gzip",
                "x-o3-app-name": app_name,
                "x-o3-app-version": "17.16.0",
                "x-o3-language": "ru",
            }
            if user_agent:
                headers["User-Agent"] = user_agent
            else:
                # web-запрос: добавляем браузерные заголовки
                headers.update({
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                    "Referer": "https://www.ozon.ru/",
                    "sec-fetch-dest": "empty",
                    "sec-fetch-mode": "cors",
                    "sec-fetch-site": "same-site",
                })

            async with _CurlSession(impersonate=profile) as sess:
                resp = await asyncio.wait_for(sess.get(url, headers=headers), timeout=8)
                if resp.status_code != 200:
                    return []
                try:
                    data = resp.json()
                except Exception:
                    return []
            return self._items_from_json(data, region, category, limit)
        except Exception as exc:
            logger.debug("[ozon] %s %s failed: %s", profile, app_name, exc)
            return []

    async def _fetch_html(self, url: str) -> str:
        """Пробуем получить HTML страницы поиска через curl_cffi."""
        for profile in ("chrome124", "chrome120", "safari17_0"):
            try:
                async with _CurlSession(impersonate=profile) as sess:
                    resp = await asyncio.wait_for(
                        sess.get(url, headers={
                            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                            "Accept-Language": "ru-RU,ru;q=0.9",
                            "Accept-Encoding": "gzip",
                            "Referer": "https://www.ozon.ru/",
                        }),
                        timeout=10,
                    )
                    if resp.status_code == 200 and len(resp.text) > 5000:
                        return resp.text
            except Exception:
                pass
        return ""

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
