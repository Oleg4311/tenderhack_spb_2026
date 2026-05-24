import asyncio
import json
import logging
import os
import re
import tempfile
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse

try:
    from curl_cffi.requests import AsyncSession
except ImportError:  # pragma: no cover - runtime image should include curl-cffi
    AsyncSession = None

from app.parsers.common import (
    ProductItem,
    SourceResult,
    calculate_relevance,
    clean_text,
    default_geo,
    normalize_price,
    normalize_url,
)

logger = logging.getLogger(__name__)


class AggregatorParser:
    source = "aggregator"

    # Price.ru search returns model nodes. The marketplace sellers often live
    # behind model offer pagination, so we enrich models before source filtering.
    max_models_to_enrich = int(os.getenv("PRICE_RU_MAX_MODELS", "12"))
    model_page_size = int(os.getenv("PRICE_RU_MODEL_PAGE_SIZE", "50"))
    max_model_pages = int(os.getenv("PRICE_RU_MAX_MODEL_PAGES", "4"))
    model_enrichment_concurrency = int(os.getenv("PRICE_RU_MODEL_CONCURRENCY", "4"))

    async def search(self, query: str, region: str = "Москва", limit: int = 10, category: str = "clothes") -> SourceResult:
        if AsyncSession is None:
            return SourceResult(
                self.source,
                "error",
                errorReason="curl_cffi is not installed",
                diagnostics={"sourceHost": "price.ru"},
            )

        effective_limit = max(limit * 3, 30)
        diagnostics: dict[str, Any] = {
            "sourceHost": "price.ru",
            "mode": "offer_graph_extractor",
            "pages": 2,
            "include_hidden_offers": True,
        }
        try:
            results = await asyncio.wait_for(
                asyncio.gather(
                    self._search_price_ru(query, region, effective_limit, category, page=1),
                    self._search_price_ru(query, region, effective_limit, category, page=2),
                    return_exceptions=True,
                ),
                timeout=35,
            )
        except asyncio.TimeoutError:
            return SourceResult(self.source, "error", errorReason="price.ru aggregator timeout", diagnostics={"sourceHost": "price.ru"})
        except Exception as exc:
            logger.info("[aggregator] price.ru failed: %s", exc)
            return SourceResult(self.source, "error", errorReason=f"price.ru aggregator error: {type(exc).__name__}", diagnostics={"sourceHost": "price.ru"})

        items: list[ProductItem] = []
        debug = self._empty_debug_metrics()
        for result in results:
            if isinstance(result, tuple):
                page_items, page_debug = result
                items.extend(page_items)
                self._merge_debug_metrics(debug, page_debug)
            elif isinstance(result, Exception):
                debug["search_errors"] = debug.get("search_errors", 0) + 1
                debug.setdefault("last_search_error", type(result).__name__)

        items = self._dedupe(items)
        diagnostics.update(debug)
        marketplace_missing = not (debug.get("wb_offers_count") or debug.get("ozon_offers_count") or debug.get("yandex_offers_count"))
        if not items or debug.get("search_errors") or marketplace_missing:
            fallback_items, fallback_diag = await self._legacy_search_fallback(query, region, limit, category)
            if fallback_items:
                items = self._dedupe(items + fallback_items)
                diagnostics["legacyFallback"] = fallback_diag
                diagnostics["fallbackUsed"] = "price.ru_search_top_offers"

        return SourceResult(
            self.source,
            "ok" if items else "empty",
            len(items),
            "",
            items,
            diagnostics=diagnostics,
        )

    async def _legacy_search_fallback(self, query: str, region: str, limit: int, category: str) -> tuple[list[ProductItem], dict[str, Any]]:
        diagnostics: dict[str, Any] = {
            "mode": "legacy_search_top_offers",
            "pages": 0,
            "items_seen": 0,
            "models_seen": 0,
            "model_top_offer_requests": 0,
            "errors": 0,
        }
        raw_items: list[dict[str, Any]] = []
        for page in (1, 2):
            try:
                page_items = await self._legacy_search_page(query, page=page, per_page=max(15, min(limit * 2, 30)))
                diagnostics["pages"] += 1
                diagnostics["items_seen"] += len(page_items)
                raw_items.extend(page_items)
            except Exception as exc:
                diagnostics["errors"] += 1
                diagnostics.setdefault("last_error", type(exc).__name__)

        expanded: list[dict[str, Any]] = []
        model_ids: list[str] = []
        for raw in raw_items:
            has_shop = isinstance(raw.get("shop_info"), dict) and raw.get("shop_info")
            if raw.get("type") == "model" and raw.get("id") and raw.get("offer_count") and not has_shop:
                model_ids.append(str(raw["id"]))
            else:
                expanded.append(raw)
        diagnostics["models_seen"] = len(model_ids)

        if model_ids:
            tasks = [self._legacy_model_top_offers(model_id) for model_id in model_ids[: min(6, len(model_ids))]]
            chunks = await asyncio.gather(*tasks, return_exceptions=True)
            for chunk in chunks:
                diagnostics["model_top_offer_requests"] += 1
                if isinstance(chunk, list):
                    expanded.extend(chunk)
                else:
                    diagnostics["errors"] += 1
                    diagnostics.setdefault("last_model_error", type(chunk).__name__)

        items: list[ProductItem] = []
        for raw in expanded:
            item = self._item_from_price_ru(raw, region, category)
            if item and item.title and (item.price or item.url):
                items.append(item)
        items = self._dedupe(items)
        diagnostics["items_returned"] = len(items)
        return items, diagnostics

    async def _legacy_search_page(self, query: str, *, page: int, per_page: int) -> list[dict[str, Any]]:
        params = {
            "region_id": 1,
            "page": page,
            "per_page": per_page,
            "adult": "false",
            "device": 1,
            "check_offers": "true",
        }
        url = "https://price.ru/v4/search?" + urlencode(params)
        data = await self._post_price_ru_json(url, {"query": query}, timeout=8)
        items = data.get("items") if isinstance(data, dict) else []
        return [item for item in items or [] if isinstance(item, dict)]

    async def _legacy_model_top_offers(self, model_id: str) -> list[dict[str, Any]]:
        params = {
            "region_id": 1,
            "page": 1,
            "per_page": 4,
            "adult": "false",
            "device": 1,
        }
        url = f"https://price.ru/v4/models/{model_id}/offers?" + urlencode(params)
        data = await self._post_price_ru_json(url, {}, timeout=6)
        offers = data.get("list") if isinstance(data, dict) else []
        out = []
        for offer in offers or []:
            if isinstance(offer, dict):
                offer.setdefault("model_id", model_id)
                out.append(offer)
        return out

    async def suggest(self, query: str, limit: int = 8) -> dict[str, Any]:
        if AsyncSession is None:
            return {"suggestions": [], "source": "local", "sourcePolicy": "curl_cffi_missing"}
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
        return {"suggestions": suggestions, "source": "price.ru", "sourcePolicy": "backend web-flow only; no external API key/sdk"}

    async def _suggest_categories(self, query: str, limit: int) -> list[str]:
        params = {"region_id": 1, "page_type": "search", "adult": "false", "device": 1}
        url = "https://price.ru/v4/search/categories?" + urlencode(params)
        data = await self._post_price_ru_json(url, {"query": query}, timeout=7)
        out = []
        for item in data.get("categories") or []:
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
        out = []
        for item in data.get("items") or []:
            if isinstance(item, dict) and item.get("name"):
                out.append(clean_text(item["name"]))
            if len(out) >= limit:
                break
        return out

    async def _post_price_ru_json(self, url: str, payload: dict[str, Any], timeout: int = 15) -> dict[str, Any]:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ru-RU,ru;q=0.9",
            "Content-Type": "application/json",
            "Origin": "https://price.ru",
            "Referer": "https://price.ru/search/",
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        async with AsyncSession(impersonate="chrome124", allow_redirects=True, max_redirects=5) as session:
            response = await session.post(url, data=body, headers=headers, timeout=timeout)
            if response.status_code >= 400:
                raise RuntimeError(f"price.ru HTTP {response.status_code}")
            return response.json()

    async def _search_price_ru(self, query: str, region: str, limit: int, category: str, page: int = 1) -> tuple[list[ProductItem], dict[str, Any]]:
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
            return [], self._empty_debug_metrics()

        raw_items, debug = await self._expand_model_items(raw_items, query=query, page=page)
        items: list[ProductItem] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            item = self._item_from_price_ru(raw, region, category)
            if item and item.title and (item.price or item.url):
                items.append(item)
        return self._dedupe(items), debug

    async def _expand_model_items(self, raw_items: list[Any], *, query: str, page: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        expanded: list[dict[str, Any]] = []
        model_refs: list[dict[str, str]] = []
        debug = self._empty_debug_metrics()
        debug["search_items_count"] = len(raw_items)

        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            has_shop = isinstance(raw.get("shop_info"), dict) and raw.get("shop_info")
            if raw.get("type") == "model" and raw.get("id") and raw.get("offer_count") and not has_shop:
                model_refs.append(
                    {
                        "id": str(raw["id"]),
                        "url": self._model_page_url(raw),
                        "offer_count": str(raw.get("offer_count") or ""),
                    }
                )
            else:
                expanded.append(raw)

        debug["models_found"] = len(model_refs)
        if not model_refs:
            debug["visible_offers_count"] = len(expanded)
            self._count_sources(expanded, debug)
            return expanded or [raw for raw in raw_items if isinstance(raw, dict)], debug

        selected = model_refs[: min(self.max_models_to_enrich, len(model_refs))]
        debug["models_enriched"] = len(selected)
        chunks = await self._enrich_model_refs(selected)

        raw_dump: list[dict[str, Any]] = []
        for chunk in chunks:
            if isinstance(chunk, dict):
                offers = chunk.get("offers") if isinstance(chunk.get("offers"), list) else []
                expanded.extend(offers)
                raw_dump.append(chunk)
                debug["total_offers_count"] += int(chunk.get("total") or len(offers) or 0)
                debug["visible_offers_count"] += len(offers)
                debug["offer_pages_loaded"] += int(chunk.get("pages_loaded") or 0)
            elif isinstance(chunk, Exception):
                debug["offer_enrichment_errors"] += 1
                debug.setdefault("last_offer_enrichment_error", type(chunk).__name__)

        self._count_sources(expanded, debug)
        self._dump_raw_offers(query=query, page=page, payload=raw_dump, debug=debug)
        if not expanded:
            fallback = [raw for raw in raw_items if isinstance(raw, dict)]
            debug["visible_offers_count"] = len(fallback)
            self._count_sources(fallback, debug)
            return fallback, debug
        return expanded, debug

    async def _enrich_model_refs(self, model_refs: list[dict[str, str]]) -> list[Any]:
        semaphore = asyncio.Semaphore(max(1, self.model_enrichment_concurrency))

        async def run(model_ref: dict[str, str]) -> Any:
            async with semaphore:
                return await self._model_offers_graph(model_ref)

        return await asyncio.gather(*(run(model_ref) for model_ref in model_refs), return_exceptions=True)

    async def _model_offers_graph(self, model_ref: dict[str, str]) -> dict[str, Any]:
        model_id = model_ref["id"]
        offers: list[dict[str, Any]] = []
        total = 0
        pages_loaded = 0

        for page in range(1, self.max_model_pages + 1):
            data = await self._model_offers_page(model_id, page=page, per_page=self.model_page_size)
            page_offers = data.get("list") if isinstance(data, dict) else []
            if not isinstance(page_offers, list):
                page_offers = []
            total = int(float(data.get("total") or data.get("offer_count") or total or len(page_offers) or 0))
            pages_loaded += 1
            for offer in page_offers:
                if isinstance(offer, dict):
                    offer.setdefault("model_id", model_id)
                    offer.setdefault("model_page_url", model_ref.get("url") or "")
                    offers.append(offer)
            if not page_offers or len(offers) >= total:
                break

        return {
            "model_id": model_id,
            "model_url": model_ref.get("url") or "",
            "expected_offer_count": model_ref.get("offer_count") or "",
            "total": total or len(offers),
            "pages_loaded": pages_loaded,
            "offers": offers,
        }

    async def _model_offers_page(self, model_id: str, *, page: int, per_page: int) -> dict[str, Any]:
        params = {
            "region_id": 1,
            "page": page,
            "per_page": max(1, min(per_page, 100)),
            "adult": "false",
            "device": 1,
            "include_hidden_offers": "true",
            "expand": "offers",
            "lazy": "true",
        }
        url = f"https://price.ru/v4/models/{model_id}/offers?" + urlencode(params)
        return await self._post_price_ru_json(url, {}, timeout=12)

    def _model_page_url(self, raw: dict[str, Any]) -> str:
        slug = clean_text(raw.get("slug") or "")
        if slug:
            return urljoin("https://price.ru/", slug.strip("/") + "/")
        model_id = clean_text(raw.get("id") or "")
        return urljoin("https://price.ru/", f"model/{model_id}/") if model_id else "https://price.ru/"

    def _item_from_price_ru(self, raw: dict[str, Any], region: str, category: str) -> ProductItem | None:
        title = clean_text(raw.get("name") or raw.get("title") or "")
        if not title:
            return None

        price_info = raw.get("price_info") if isinstance(raw.get("price_info"), dict) else {}
        price = normalize_price(raw.get("price") or raw.get("min_price") or raw.get("price_min") or price_info.get("min") or raw.get("cost"))
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
        if raw.get("model_page_url"):
            chars.setdefault("modelUrl", clean_text(raw.get("model_page_url")))
        if click_url:
            chars.setdefault("merchantRedirect", urljoin("https://price.ru/", click_url))

        item_source, source_type, source_host = self._classify_source(raw, shop_info)
        return ProductItem(
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

    def _classify_source(self, raw: dict[str, Any], shop_info: dict[str, Any]) -> tuple[str, str, str]:
        site = clean_text(shop_info.get("site") or "")
        shop_name = clean_text(shop_info.get("name") or raw.get("shop_name") or "")
        slug = clean_text(shop_info.get("slug") or raw.get("slug") or "")
        redirect = clean_text(raw.get("redirect_target") or raw.get("click_url") or "")
        text = " ".join([site, shop_name, slug, redirect]).lower()

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
            key = (item.source, item.title.lower(), item.price, item.seller.lower())
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

    def _empty_debug_metrics(self) -> dict[str, Any]:
        return {
            "search_items_count": 0,
            "models_found": 0,
            "models_enriched": 0,
            "total_offers_count": 0,
            "visible_offers_count": 0,
            "offer_pages_loaded": 0,
            "offer_enrichment_errors": 0,
            "ozon_offers_count": 0,
            "wb_offers_count": 0,
            "yandex_offers_count": 0,
        }

    def _merge_debug_metrics(self, target: dict[str, Any], source: dict[str, Any]) -> None:
        for key, value in source.items():
            if isinstance(value, (int, float)):
                target[key] = target.get(key, 0) + value
            elif key not in target:
                target[key] = value

    def _count_sources(self, offers: list[dict[str, Any]], debug: dict[str, Any]) -> None:
        for raw in offers:
            shop_info = raw.get("shop_info") if isinstance(raw.get("shop_info"), dict) else {}
            source, _, _ = self._classify_source(raw, shop_info)
            if source == "ozon":
                debug["ozon_offers_count"] += 1
            elif source == "wildberries":
                debug["wb_offers_count"] += 1
            elif source == "yandex_market":
                debug["yandex_offers_count"] += 1

    def _dump_raw_offers(self, *, query: str, page: int, payload: list[dict[str, Any]], debug: dict[str, Any]) -> None:
        if os.getenv("PRICE_RU_DUMP_RAW_OFFERS", "1").strip().lower() in {"0", "false", "no"}:
            return
        try:
            dump_dir = os.getenv("PRICE_RU_DUMP_DIR", tempfile.gettempdir())
            os.makedirs(dump_dir, exist_ok=True)
            safe_query = re.sub(r"[^a-zA-Z0-9а-яА-Я_-]+", "_", query).strip("_")[:60] or "query"
            path = os.path.join(dump_dir, f"price_ru_raw_offers_{safe_query}_p{page}.json")
            slim_payload = []
            for model in payload[: self.max_models_to_enrich]:
                slim_payload.append(
                    {
                        "model_id": model.get("model_id"),
                        "model_url": model.get("model_url"),
                        "total": model.get("total"),
                        "pages_loaded": model.get("pages_loaded"),
                        "offers": model.get("offers", [])[: self.model_page_size * self.max_model_pages],
                    }
                )
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"debug": debug, "models": slim_payload}, fh, ensure_ascii=False)
            debug["raw_offers_dump"] = path
        except Exception as exc:
            debug["raw_offers_dump_error"] = type(exc).__name__
