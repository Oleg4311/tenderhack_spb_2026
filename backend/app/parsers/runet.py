import asyncio
import logging
import random
import re
from urllib.parse import quote_plus, urlparse
from xml.etree import ElementTree

from app.parsers.browser import fetch_rendered_html
from app.parsers.common import ProductItem, SourceResult, clean_text, default_geo, normalize_price
from app.parsers.extractors import extract_product_from_html, extract_product_links, find_products_in_json
from app.parsers.http_client import Fetcher, browser_headers

logger = logging.getLogger(__name__)

SITE_POOL = {
    "tires": ["4tochki.ru", "kolesa.ru", "autoopt.ru", "shinexp.ru"],
    "office": ["foroffice.ru", "oldi.ru", "price.ru", "komus.ru"],
    "clothes": ["zolla.com", "sportmaster.ru", "kari.com", "sela.ru"],
}

# Normalize user-supplied category to pool keys
_CATEGORY_ALIASES: dict[str, str] = {
    "clothes": "clothes", "clothing": "clothes", "apparel": "clothes", "fashion": "clothes",
    "одежда": "clothes", "обувь": "clothes", "одежда и обувь": "clothes",
    "tires": "tires", "tyres": "tires", "шины": "tires", "резина": "tires",
    "office": "office", "офис": "office", "канцелярия": "office", "офисная техника": "office",
    "электроника": "office", "electronics": "office",
}


def _resolve_category(category: str) -> str:
    return _CATEGORY_ALIASES.get((category or "").lower().strip(), "tires")

SEARCH_PATTERNS = [
    "https://{host}/search/?q={q}",
    "https://{host}/search/?text={q}",
    "https://{host}/catalog/?q={q}",
    "https://{host}/?s={q}",
]

ADAPTERS = {
    # ── Tires ──────────────────────────────────────────────────────────────────
    "4tochki.ru": {
        "search": ["https://4tochki.ru/search/?q={q}", "https://4tochki.ru/catalog/tyres/?q={q}"],
        "allow": ["/catalog/tires/", "/catalog/tyres/", "/products/tyres/", "/tyres/"],
        "brand_patterns": [r"Бренд[:\s]+([^;,.|]{2,60})", r"Производитель[:\s]+([^;,.|]{2,60})"],
        "stock_patterns": [r"(?:наличие|остаток|склад)[:\s]+([^.!?]{2,80})"],
    },
    "kolesa.ru": {
        "search": [
            "https://kolesa.ru/tyres/?q={q}",
            "https://kolesa.ru/tyres/?search={q}",
        ],
        "allow": ["/tyres/", "/shiny/", "/product/", "/item/"],
        "seller": "kolesa.ru",
    },
    "autoopt.ru": {
        "search": ["https://autoopt.ru/search/?q={q}", "https://autoopt.ru/catalog/?text={q}"],
        "allow": ["/catalog/", "/product/", "/item/"],
        "seller": "АвтоОпт",
    },
    "shinexp.ru": {
        "search": [
            "https://shinexp.ru/search/?q={q}",
            "https://shinexp.ru/catalog/?q={q}",
        ],
        "allow": ["/catalog/", "/product/", "/item/"],
        "seller": "ShineXP",
    },
    # ── Office / Electronics ───────────────────────────────────────────────────
    "foroffice.ru": {
        "search": ["https://www.foroffice.ru/search/?q={q}", "https://foroffice.ru/search/?q={q}"],
        "allow": ["/products/", "/catalog/"],
        "brand_patterns": [r"Производитель[:\s]+([^;,.|]{2,60})", r"Бренд[:\s]+([^;,.|]{2,60})"],
        "seller": "foroffice.ru",
    },
    "oldi.ru": {
        "search": ["https://www.oldi.ru/search/?text={q}", "https://oldi.ru/search/?text={q}"],
        "allow": ["/catalog/element/", "/catalog/"],
        "brand_patterns": [r"Бренд[:\s]+([^;,.|]{2,60})", r"Производитель[:\s]+([^;,.|]{2,60})"],
        "seller": "OLDI",
    },
    "price.ru": {
        "search": ["https://price.ru/search/?query={q}", "https://price.ru/search/?q={q}"],
        "allow": ["/product/", "/offers/"],
        "seller_patterns": [r"Магазин[:\s]+([^.!?]{2,80})", r"Продавец[:\s]+([^.!?]{2,80})"],
    },
    "komus.ru": {
        "search": [
            "https://www.komus.ru/search/?q={q}",
            "https://www.komus.ru/katalog/?search={q}",
        ],
        "allow": ["/product/", "/g/"],
        "brand_patterns": [r"Бренд[:\s]+([^;,.|]{2,60})", r"Производитель[:\s]+([^;,.|]{2,60})"],
        "seller": "Комус",
    },
    # ── Clothes / Shoes ────────────────────────────────────────────────────────
    "zolla.com": {
        "search": [
            "https://zolla.com/search/?q={q}",
            "https://zolla.com/catalog/search/?q={q}",
        ],
        "allow": ["/product/"],
        "brand_patterns": [r"Бренд[:\s]+([^;,.|]{2,60})", r"Производитель[:\s]+([^;,.|]{2,60})"],
        "seller": "Zolla",
    },
    "sportmaster.ru": {
        "preflight": "https://www.sportmaster.ru/",
        "search": [
            "https://www.sportmaster.ru/catalog/search/?text={q}",
            "https://www.sportmaster.ru/search/?text={q}",
        ],
        "allow": ["/product/"],
        "brand_patterns": [r"Бренд[:\s]+([^;,.|]{2,60})", r"Производитель[:\s]+([^;,.|]{2,60})"],
        "seller": "Спортмастер",
    },
    "kari.com": {
        "preflight": "https://kari.com/",
        "search": [
            "https://kari.com/catalog/?q={q}",
            "https://kari.com/search/?q={q}",
        ],
        "allow": ["/catalog/product/", "/product/", "/catalog/"],
        "brand_patterns": [r"Бренд[:\s]+([^;,.|]{2,60})"],
        "seller": "kari",
    },
    "sela.ru": {
        "preflight": "https://sela.ru/",
        "search": [
            "https://sela.ru/search/?q={q}",
            "https://sela.ru/catalog/?q={q}",
        ],
        "allow": ["/product/", "/catalog/"],
        "brand_patterns": [r"Бренд[:\s]+([^;,.|]{2,60})"],
        "seller": "SELA",
    },
}


# Sites that always block plain HTTP — go straight to Playwright, skip the failed HTTP round-trip.
BROWSER_FIRST_HOSTS = {"sportmaster.ru", "kari.com", "4tochki.ru", "sela.ru"}


class RunetParser:
    source = "runet"

    async def search(self, query: str, region: str = "Москва", limit: int = 10, category: str = "tires") -> SourceResult:
        cat_key = _resolve_category(category)
        hosts = SITE_POOL.get(cat_key, SITE_POOL["tires"])
        per_host = max(1, limit // max(1, len(hosts)) + 1)
        logger.info("[runet] query=%r category=%s cat_key=%s hosts=%s", query, category, cat_key, hosts)
        async with Fetcher() as fetcher:
            tasks = [
                asyncio.wait_for(
                    self._search_host(fetcher, host, query, region, category, per_host),
                    timeout=30,
                )
                for host in hosts
            ]
            chunks = await asyncio.gather(*tasks, return_exceptions=True)
        items: list[ProductItem] = []
        for host, chunk in zip(hosts, chunks):
            if isinstance(chunk, list):
                logger.info("[runet] host=%s found=%d", host, len(chunk))
                items.extend(chunk)
            elif isinstance(chunk, Exception):
                logger.info("[runet] host=%s error=%s", host, type(chunk).__name__)
        items = self._dedupe(items)[:limit]
        logger.info("[runet] total_items=%d", len(items))
        return SourceResult(self.source, "ok" if items else "empty", len(items), "", items)

    async def _search_host(self, fetcher: Fetcher, host: str, query: str, region: str, category: str, limit: int) -> list[ProductItem]:
        items: list[ProductItem] = []
        q = quote_plus(query)
        html = ""
        base_url = f"https://{host}/"
        adapter = ADAPTERS.get(host, {})
        links: list[str] = []
        # Preflight: some sites (e.g. sportmaster) require a homepage visit for session cookies
        if adapter.get("preflight"):
            try:
                await asyncio.wait_for(
                    fetcher.get_text(adapter["preflight"], source=self.source, headers=browser_headers(referer="https://www.google.com/", source=self.source), retries=0),
                    timeout=5,
                )
            except Exception:
                pass

        patterns = adapter.get("search") or SEARCH_PATTERNS
        for pattern in patterns:
            url = pattern.format(host=host, q=q)
            # Browser-first: skip the HTTP round-trip for hosts that always block it
            should_try_browser = host in BROWSER_FIRST_HOSTS
            html = ""
            if not should_try_browser:
                try:
                    resp = await asyncio.wait_for(
                        fetcher.get_text(url, source=self.source, headers=browser_headers(referer=base_url, source=self.source), retries=0),
                        timeout=8,
                    )
                    if resp.blocked:
                        should_try_browser = True
                    else:
                        html = resp.text or ""
                except Exception:
                    should_try_browser = True
            if should_try_browser:
                try:
                    rendered = await asyncio.wait_for(
                        fetch_rendered_html(
                            url,
                            referer=base_url,
                            wait_selectors=['a[href*="/catalog/"]', 'a[href*="/product"]', ".product", ".item", "article"],
                            scroll_steps=2,
                        ),
                        timeout=14,
                    )
                except Exception:
                    rendered = None
                if rendered:
                    html = rendered.html or ""
                    # XHR-first: extract products directly from captured API payloads
                    if rendered.product_payloads:
                        logger.info("[runet] host=%s xhr_payloads=%d", host, len(rendered.product_payloads))
                        for payload in rendered.product_payloads:
                            for raw in find_products_in_json(payload)[:limit]:
                                item = self._item_from_raw(raw, host, base_url, region, category, adapter)
                                if item:
                                    items.append(item)
                        if items:
                            return items[:limit]
            links = self._filter_links(extract_product_links(html, url), host, adapter)
            logger.info("[runet] host=%s pattern=%s html_links=%d", host, url, len(links))
            if links:
                break
        if not links:
            try:
                links = await asyncio.wait_for(self._discover_links_from_sitemaps(fetcher, host, query, category, limit * 3), timeout=4)
            except Exception:
                links = []
        for i, link in enumerate(links[:limit]):
            if host in BROWSER_FIRST_HOSTS:
                # Human-like pause between product page visits; first page has no delay
                if i > 0:
                    await asyncio.sleep(random.uniform(0.8, 2.0))
                try:
                    rendered = await asyncio.wait_for(
                        fetch_rendered_html(link, referer=base_url, wait_selectors=["h1", ".product", ".price"], scroll_steps=1),
                        timeout=8,
                    )
                except Exception:
                    continue
                if not rendered or rendered.status in ("blocked", "error") or not rendered.html:
                    continue
                detail_html = rendered.html
            else:
                try:
                    detail = await asyncio.wait_for(
                        fetcher.get_text(link, source=self.source, referer=base_url, retries=0),
                        timeout=8,
                    )
                except Exception:
                    continue
                detail_html = detail.text
                if detail.blocked or not detail_html:
                    try:
                        rendered = await asyncio.wait_for(
                            fetch_rendered_html(link, referer=base_url, wait_selectors=["h1", ".product", ".price"], scroll_steps=1),
                            timeout=5,
                        )
                    except Exception:
                        rendered = None
                    if not rendered:
                        continue
                    if rendered.status == "blocked" or not rendered.html:
                        continue
                    detail_html = rendered.html
            item = extract_product_from_html(detail_html, link, self.source)
            if not item.title and not item.price:
                continue
            item.source = self.source
            item.sourceType = "runet"
            item.realSourceHost = urlparse(link).netloc
            item.category = category
            item.region = region
            item.geo = default_geo(region) | {k: v for k, v in item.geo.items() if v}
            self._apply_host_adapter(item, detail_html, adapter)
            self._enrich_geo_availability(item, detail_html, region)
            items.append(item)
            if len(items) >= limit:
                break
        return items

    def _item_from_raw(self, raw: dict, host: str, base_url: str, region: str, category: str, adapter: dict) -> ProductItem | None:
        from app.parsers.common import normalize_url
        title = (raw.get("name") or raw.get("title") or raw.get("goodsName") or raw.get("displayName") or "").strip()
        if not title:
            return None
        price = normalize_price(raw.get("price") or raw.get("salePrice") or raw.get("finalPrice") or raw.get("priceValue") or 0)
        link = raw.get("url") or raw.get("link") or raw.get("productUrl") or raw.get("href") or ""
        if link:
            link = normalize_url(link, base_url)
        image = raw.get("image") or raw.get("imageUrl") or raw.get("picture") or raw.get("img") or ""
        if image and not image.startswith("http"):
            image = normalize_url(image, base_url)
        brand = (raw.get("brand") or raw.get("brandName") or "")
        if isinstance(brand, dict):
            brand = brand.get("name") or ""
        item = ProductItem(
            source=self.source,
            sourceType="runet",
            realSourceHost=host,
            title=clean_text(title),
            brand=clean_text(brand),
            price=price,
            url=link,
            mainImage=str(image),
            images=[str(image)] if image else [],
            category=category,
            region=region,
            geo=default_geo(region),
        )
        if adapter.get("seller") and not item.seller:
            item.seller = adapter["seller"]
        return item

    def _apply_host_adapter(self, item: ProductItem, html: str, adapter: dict) -> None:
        text = clean_text(re.sub(r"<[^>]+>", " ", html or ""))
        if adapter.get("seller") and not item.seller:
            item.seller = adapter["seller"]
        for pattern in adapter.get("seller_patterns", []):
            match = re.search(pattern, text, re.I)
            if match and not item.seller:
                item.seller = clean_text(match.group(1))
        for pattern in adapter.get("brand_patterns", []):
            match = re.search(pattern, text, re.I)
            if match and not item.brand:
                item.brand = clean_text(match.group(1))
                item.characteristics.setdefault("brand", item.brand)
        for pattern in adapter.get("stock_patterns", []):
            match = re.search(pattern, text, re.I)
            if match:
                item.characteristics.setdefault("stockRaw", clean_text(match.group(1)))
        if not item.price:
            price_match = re.search(r"(?:цена|стоимость)[^\d]{0,20}(\d[\d\s]{2,12})\s*(?:₽|руб)", text, re.I)
            if price_match:
                item.price = normalize_price(price_match.group(1))
        coord_match = re.search(r"(?<!\d)([45]\d\.\d{3,}|6[0-9]\.\d{3,})[,;\s]+([3-5]\d\.\d{3,})", text)
        if coord_match and item.geo.get("latitude") is None:
            try:
                item.geo["latitude"] = float(coord_match.group(1))
                item.geo["longitude"] = float(coord_match.group(2))
            except Exception:
                pass

    async def _discover_links_from_sitemaps(self, fetcher: Fetcher, host: str, query: str, category: str, limit: int) -> list[str]:
        sitemap_urls = [f"https://{host}/sitemap.xml", f"https://{host}/sitemap_index.xml"]
        robots = await fetcher.get_text(f"https://{host}/robots.txt", source=self.source, retries=0)
        if robots.text:
            for match in re.findall(r"(?im)^sitemap:\s*(\S+)", robots.text):
                if match not in sitemap_urls:
                    sitemap_urls.append(match.strip())

        found: list[str] = []
        query_tokens = [t.lower() for t in re.findall(r"[a-zа-яё0-9]+", query, re.I) if len(t) > 2]
        category_hints = {
            "tires": ("shin", "tyre", "tire", "шины", "rezin", "catalog"),
            "office": ("printer", "mfu", "office", "орг", "canon", "hp", "catalog"),
            "clothes": ("odezh", "clothes", "kurt", "futbol", "sneaker", "catalog", "apparel", "fashion"),
        }.get(category, ("product", "catalog"))

        seen_sitemaps: set[str] = set()
        queue = sitemap_urls[:8]
        while queue and len(found) < limit:
            sitemap = queue.pop(0)
            if sitemap in seen_sitemaps:
                continue
            seen_sitemaps.add(sitemap)
            resp = await fetcher.get_text(sitemap, source=self.source, retries=0)
            if not resp.text or resp.blocked:
                continue
            urls = self._parse_sitemap_urls(resp.text)
            nested = [u for u in urls if "sitemap" in u.lower() and u not in seen_sitemaps]
            queue.extend(nested[:10])
            for url in urls:
                lower = url.lower()
                if urlparse(url).netloc and not urlparse(url).netloc.endswith(host):
                    continue
                if not any(hint in lower for hint in category_hints):
                    continue
                if query_tokens and not any(token in lower for token in query_tokens):
                    if len(found) > limit // 2:
                        continue
                if url not in found:
                    found.append(url)
                if len(found) >= limit:
                    break
        return found[:limit]

    def _parse_sitemap_urls(self, xml_text: str) -> list[str]:
        urls: list[str] = []
        try:
            root = ElementTree.fromstring(xml_text.encode("utf-8"))
            for loc in root.iter():
                if loc.tag.lower().endswith("loc") and loc.text:
                    urls.append(loc.text.strip())
        except Exception:
            urls.extend(re.findall(r"<loc>\s*(.*?)\s*</loc>", xml_text, re.I | re.S))
        return urls[:1000]

    def _filter_links(self, links: list[str], host: str, adapter: dict) -> list[str]:
        allow = adapter.get("allow") or []
        out: list[str] = []
        for link in links:
            parsed = urlparse(link)
            if not parsed.netloc.endswith(host):
                continue
            if allow and not any(part in parsed.path for part in allow):
                continue
            if link not in out:
                out.append(link)
        return out

    def _enrich_geo_availability(self, item: ProductItem, html: str, region: str) -> None:
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or " ")).strip()
        lower = text.lower()
        if not item.availability:
            if any(marker in lower for marker in ("в наличии", "есть в наличии", "доступно", "на складе")):
                item.availability = "in_stock"
            elif any(marker in lower for marker in ("нет в наличии", "под заказ", "ожидается")):
                item.availability = "limited_or_out_of_stock"
        if not item.deliveryInfo:
            delivery = re.search(r"(доставк[а-яё\s]{0,30}(?:сегодня|завтра|от\s+\d+|[0-9]+\s*дн)[^.!?]{0,120})", text, re.I)
            if delivery:
                item.deliveryInfo = delivery.group(1).strip()
        address = re.search(r"((?:г\.?\s*)?(?:Москва|Санкт-Петербург|Новосибирск|Екатеринбург|Казань)[^.!?]{0,140}(?:ул\.|улица|пр-т|проспект|шоссе|д\.|дом)[^.!?]{0,160})", text, re.I)
        if address:
            item.geo["storeAddress"] = address.group(1).strip()
            item.geo["pickupAddress"] = item.geo.get("pickupAddress") or address.group(1).strip()
        if region and not item.geo.get("deliveryRegion"):
            item.geo["deliveryRegion"] = region
        city = re.search(r"(Москва|Санкт-Петербург|Новосибирск|Екатеринбург|Казань|Краснодар|Самара|Уфа)", text, re.I)
        if city:
            item.geo["detectedRegion"] = city.group(1)
            item.geo["city"] = city.group(1)

    def _dedupe(self, items: list[ProductItem]) -> list[ProductItem]:
        seen, out = set(), []
        for item in items:
            key = (item.url or f"{item.realSourceHost}:{item.title}").split("?")[0]
            if key and key not in seen:
                seen.add(key)
                out.append(item)
        return out
