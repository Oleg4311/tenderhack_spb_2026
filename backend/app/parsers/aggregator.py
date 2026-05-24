import asyncio
import logging
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse

try:
    from curl_cffi.requests import AsyncSession
except ImportError:  # pragma: no cover - runtime image should include curl-cffi
    AsyncSession = None

from app.parsers.common import ProductItem, SourceResult, calculate_relevance, clean_text, default_geo, normalize_price, normalize_url

logger = logging.getLogger(__name__)


class AggregatorParser:
    source = "aggregator"

    async def search(self, query: str, region: str = "Москва", limit: int = 10, category: str = "clothes") -> SourceResult:
        if AsyncSession is None:
            return SourceResult(
                self.source,
                "error",
                errorReason="curl_cffi is not installed",
                diagnostics={"sourceHost": "price.ru"},
            )

        # Run two parallel searches: page 1 and page 2 for more variety
        effective_limit = max(limit, 20)
        try:
            results = await asyncio.wait_for(
                asyncio.gather(
                    self._search_price_ru(query, region, effective_limit, category, page=1),
                    self._search_price_ru(query, region, effective_limit, category, page=2),
                    return_exceptions=True,
                ),
                timeout=20,
            )
        except asyncio.TimeoutError:
            return SourceResult(self.source, "error", errorReason="price.ru aggregator timeout", diagnostics={"sourceHost": "price.ru"})
        except Exception as exc:
            logger.info("[aggregator] price.ru failed: %s", exc)
            return SourceResult(self.source, "error", errorReason=f"price.ru aggregator error: {type(exc).__name__}", diagnostics={"sourceHost": "price.ru"})

        items: list[ProductItem] = []
        for result in results:
            if isinstance(result, list):
                items.extend(result)
        items = self._dedupe(items)

        status = "ok" if items else "empty"
        return SourceResult(
            self.source,
            status,
            len(items),
            "",
            items,
            diagnostics={"sourceHost": "price.ru", "mode": "internal_search_json", "pages": 2},
        )

    async def suggest(self, query: str, limit: int = 8) -> dict[str, Any]:
        if AsyncSession is None:
            return {
                "suggestions": [],
                "source": "local",
                "sourcePolicy": "curl_cffi_missing",
            }
        query = clean_text(query)
        if not query:
            return {"suggestions": [], "source": "local"}

        try:
            categories, products = await asyncio.wait_for(
                asyncio.gather(
                    self._suggest_categories(query, limit),
                    self._suggest_products(query, max(3, limit // 2)),
                ),
                timeout=8,
            )
        except Exception as exc:
            logger.info("[aggregator] suggest failed: %s", exc)
            return {
                "suggestions": [],
                "source": "price.ru",
                "errorReason": type(exc).__name__,
                "sourcePolicy": "backend web-flow only; no external API key/sdk",
            }

        seen = set()
        suggestions: list[str] = []
        for value in categories + products:
            text = clean_text(value)
            key = text.lower()
            if calculate_relevance(query, text) < 0.08:
                continue
            if not text or key in seen:
                continue
            seen.add(key)
            suggestions.append(text)
            if len(suggestions) >= limit:
                break
        return {
            "suggestions": suggestions,
            "source": "price.ru",
            "sourcePolicy": "backend web-flow only; no external API key/sdk",
        }

    async def _suggest_categories(self, query: str, limit: int) -> list[str]:
        params = {"region_id": 1, "page_type": "search", "adult": "false", "device": 1}
        url = "https://price.ru/v4/search/categories?" + urlencode(params)
        data = await self._post_price_ru_json(url, {"query": query}, timeout=7)
        categories = data.get("categories") if isinstance(data, dict) else []
        out = []
        for item in categories or []:
            if isinstance(item, dict) and item.get("title"):
                out.append(clean_text(item["title"]))
            if len(out) >= limit:
                break
        return out

    async def _suggest_products(self, query: str, limit: int) -> list[str]:
        params = {
            "region_id": 1,
            "page": 1,
            "per_page": max(4, min(limit, 10)),
            "adult": "false",
            "device": 1,
            "check_offers": "true",
        }
        url = "https://price.ru/v4/search?" + urlencode(params)
        data = await self._post_price_ru_json(url, {"query": query}, timeout=8)
        raw_items = data.get("items") if isinstance(data, dict) else []
        out = []
        for item in raw_items or []:
            if isinstance(item, dict) and item.get("name"):
                out.append(clean_text(item["name"]))
            if len(out) >= limit:
                break
        return out

    async def _post_price_ru_json(self, url: str, payload: dict[str, Any], timeout: int = 15) -> dict[str, Any]:
        import json as _json
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ru-RU,ru;q=0.9",
            "Content-Type": "application/json",
            "Origin": "https://price.ru",
            "Referer": "https://price.ru/search/",
        }
        # curl_cffi требует bytes для тела с кириллицей — json= кодирует в latin-1 и падает
        body = _json.dumps(payload, ensure_ascii=False).encode("utf-8")
        async with AsyncSession(impersonate="chrome124", allow_redirects=True, max_redirects=5) as session:
            response = await session.post(url, data=body, headers=headers, timeout=timeout)
            if response.status_code >= 400:
                raise RuntimeError(f"price.ru HTTP {response.status_code}")
            return response.json()

    async def _search_price_ru(self, query: str, region: str, limit: int, category: str, page: int = 1) -> list[ProductItem]:
        params = {
            "region_id": 1,
            "page": page,
            "per_page": min(30, max(15, limit)),
            "adult": "false",
            "device": 1,
            "check_offers": "true",
        }
        url = "https://price.ru/v4/search?" + urlencode(params)
        data = await self._post_price_ru_json(url, {"query": query}, timeout=15)

        raw_items = data.get("items") if isinstance(data, dict) else []
        if not isinstance(raw_items, list):
            return []

        raw_items = await self._expand_model_items(raw_items, limit)
        items: list[ProductItem] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            item = self._item_from_price_ru(raw, region, category)
            if item and item.title and (item.price or item.url):
                items.append(item)
            if len(items) >= limit:
                break
        return self._dedupe(items)[:limit]

    async def _expand_model_items(self, raw_items: list[Any], limit: int) -> list[dict[str, Any]]:
        expanded: list[dict[str, Any]] = []
        model_ids: list[str] = []
        for raw in raw_items[: max(limit, 12)]:
            if not isinstance(raw, dict):
                continue
            has_shop = isinstance(raw.get("shop_info"), dict) and raw.get("shop_info")
            if raw.get("type") == "model" and raw.get("id") and raw.get("offer_count") and not has_shop:
                model_ids.append(str(raw["id"]))
            else:
                expanded.append(raw)

        if not model_ids:
            return expanded or [raw for raw in raw_items if isinstance(raw, dict)]

        tasks = [self._model_offers(model_id, per_model=4) for model_id in model_ids[: min(5, len(model_ids))]]
        chunks = await asyncio.gather(*tasks, return_exceptions=True)
        for chunk in chunks:
            if isinstance(chunk, list):
                expanded.extend(chunk)
        if not expanded:
            return [raw for raw in raw_items if isinstance(raw, dict)]
        return expanded

    async def _model_offers(self, model_id: str, per_model: int = 4) -> list[dict[str, Any]]:
        params = {
            "region_id": 1,
            "page": 1,
            "per_page": max(1, min(per_model, 10)),
            "adult": "false",
            "device": 1,
        }
        url = f"https://price.ru/v4/models/{model_id}/offers?" + urlencode(params)
        data = await self._post_price_ru_json(url, {}, timeout=9)
        offers = data.get("list") if isinstance(data, dict) else []
        return [offer for offer in offers or [] if isinstance(offer, dict)]

    def _item_from_price_ru(self, raw: dict[str, Any], region: str, category: str) -> ProductItem | None:
        title = clean_text(raw.get("name") or raw.get("title") or "")
        if not title:
            return None

        price_info = raw.get("price_info") if isinstance(raw.get("price_info"), dict) else {}
        price = normalize_price(
            raw.get("price")
            or raw.get("min_price")
            or raw.get("price_min")
            or price_info.get("min")
            or raw.get("cost")
        )
        image = normalize_url(str(raw.get("image") or ""), "https://price.ru/")
        images = [image] if image else []
        for img in raw.get("additional_images") or []:
            img_url = normalize_url(str(img), "https://price.ru/")
            if img_url and img_url not in images:
                images.append(img_url)

        slug = clean_text(raw.get("slug") or "")
        click_url = clean_text(raw.get("click_url") or "")
        if slug:
            product_url = urljoin("https://price.ru/", slug.strip("/") + "/")
        elif click_url:
            product_url = urljoin("https://price.ru/", click_url)
        else:
            product_url = "https://price.ru/search/"

        shop_info = raw.get("shop_info") if isinstance(raw.get("shop_info"), dict) else {}
        model_info = raw.get("model_info") if isinstance(raw.get("model_info"), dict) else {}
        params = raw.get("params") if isinstance(raw.get("params"), dict) else {}
        attrs = raw.get("modification_attributes") if isinstance(raw.get("modification_attributes"), dict) else {}
        specifications = raw.get("specifications") if isinstance(raw.get("specifications"), list) else []

        chars = self._characteristics(raw, params, attrs, model_info, specifications)
        if price_info:
            for key in ("min", "max", "avg"):
                if price_info.get(key):
                    chars.setdefault(f"price_{key}", str(price_info[key]))
        if raw.get("type"):
            chars.setdefault("aggregatorType", clean_text(raw.get("type")))
        if raw.get("model_id"):
            chars.setdefault("modelId", str(raw.get("model_id")))
        if click_url:
            chars.setdefault("merchantRedirect", urljoin("https://price.ru/", click_url))

        item_source, source_type, source_host = self._classify_source(raw, shop_info)
        item = ProductItem(
            source=item_source,
            sourceType=source_type,
            realSourceHost=source_host,
            title=title,
            brand=clean_text(model_info.get("brand") or raw.get("brand") or ""),
            sku=str(raw.get("id") or ""),
            productId=str(raw.get("model_id") or raw.get("id") or ""),
            category=category,
            price=price,
            discountPercent=normalize_price(raw.get("discount")),
            currency="RUB",
            availability=clean_text(raw.get("availability") or ""),
            seller=clean_text(shop_info.get("name") or raw.get("shop_name") or "Price.ru"),
            rating=self._rating(raw.get("rating")),
            reviewsCount=int(float(raw.get("review_count") or raw.get("reviews_count") or 0)),
            images=images,
            mainImage=images[0] if images else "",
            url=product_url,
            characteristics=chars,
            description=clean_text(raw.get("description") or "")[:2000],
            deliveryInfo=self._delivery_text(raw),
            region=region,
            geo=default_geo(region),
        )
        return item

    def _classify_source(self, raw: dict[str, Any], shop_info: dict[str, Any]) -> tuple[str, str, str]:
        site = clean_text(shop_info.get("site") or "")
        shop_name = clean_text(shop_info.get("name") or raw.get("shop_name") or "")
        slug = clean_text(shop_info.get("slug") or raw.get("slug") or "")
        text = " ".join([site, shop_name, slug, clean_text(raw.get("redirect_target") or "")]).lower()

        if "wildberries" in text or "wb.ru" in text or text.startswith("wb ") or " wb" in text:
            return "wildberries", "marketplace", "wildberries.ru"
        if "ozon" in text or "озон" in text:
            return "ozon", "marketplace", "ozon.ru"
        if "market.yandex" in text or "yandex market" in text or "яндекс маркет" in text or "яндекс.маркет" in text:
            return "yandex_market", "marketplace", "market.yandex.ru"

        host = urlparse(site).netloc.replace("www.", "") if site else "price.ru"
        return "runet", "runet", host or "price.ru"

    def _characteristics(self, raw: dict[str, Any], *groups: Any) -> dict[str, Any]:
        chars: dict[str, Any] = {"aggregatorSource": "price.ru"}
        for group in groups:
            if isinstance(group, dict):
                for key, value in group.items():
                    text_key = clean_text(key)
                    text_value = clean_text(value)
                    if text_key and text_value and len(text_key) <= 80 and len(text_value) <= 500:
                        chars.setdefault(text_key, text_value)
            elif isinstance(group, list):
                for entry in group[:30]:
                    if isinstance(entry, dict):
                        name = entry.get("name") or entry.get("title") or entry.get("key")
                        value = entry.get("value") or entry.get("text")
                        text_key = clean_text(name)
                        text_value = clean_text(value)
                        if text_key and text_value:
                            chars.setdefault(text_key, text_value)
        if raw.get("sales_note"):
            chars.setdefault("salesNote", clean_text(raw.get("sales_note"))[:500])
        return chars

    def _delivery_text(self, raw: dict[str, Any]) -> str:
        parts = []
        for key in ("delivery", "pickup"):
            value = raw.get(key)
            if isinstance(value, dict):
                text = clean_text(value.get("description") or value.get("name") or value.get("price") or "")
            else:
                text = clean_text(value)
            if text:
                parts.append(text)
        return "; ".join(parts)[:500]

    def _dedupe(self, items: list[ProductItem]) -> list[ProductItem]:
        seen = set()
        out = []
        for item in items:
            key = (item.title.lower(), item.price, item.seller.lower())
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
        return out

    def _rating(self, value: Any) -> float:
        try:
            rating = float(value or 0)
        except Exception:
            return 0
        if rating > 5:
            rating /= 20
        return round(min(max(rating, 0), 5), 2)
