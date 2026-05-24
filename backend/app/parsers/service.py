import asyncio
import copy
import time
from typing import Any

from app.parsers.common import SOURCE_KEYS, SourceResult, calculate_completeness, calculate_relevance, relevance_breakdown
from app.parsers.aggregator import AggregatorParser
from app.parsers.ozon import OzonParser
from app.parsers.query_normalizer import detect_category_from_query, expand_query, normalize_query
from app.parsers.runet import RunetParser, _resolve_category
from app.parsers.wildberries import WildberriesParser
from app.parsers.yandex_market import YandexMarketParser

PARSERS = {
    "wildberries": WildberriesParser,
    "ozon": OzonParser,
    "yandex_market": YandexMarketParser,
    "runet": RunetParser,
}

HEALTH: dict[str, dict[str, Any]] = {
    source: {"source": source, "status": "unknown", "lastError": "", "lastLatencyMs": 0, "lastItemsCount": 0}
    for source in SOURCE_KEYS
}

_ALL_SOURCES = ("wildberries", "ozon", "yandex_market", "runet")
CATEGORY_SOURCES = {
    "clothes": _ALL_SOURCES,
    "tires": _ALL_SOURCES,
    "office": _ALL_SOURCES,
}

_SOURCE_CACHE: dict[tuple[str, str, str, str], SourceResult] = {}


def _cache_key(source: str, query: str, category: str, region: str) -> tuple[str, str, str, str]:
    return (source, query, category, region)


def _clone_result(result: SourceResult) -> SourceResult:
    return copy.deepcopy(result)


def _cache_get(source: str, query: str, category: str, region: str) -> SourceResult | None:
    cached = _SOURCE_CACHE.get(_cache_key(source, query, category, region))
    if cached and cached.items:
        result = _clone_result(cached)
        result.errorReason = ""
        result.status = "ok"
        result.diagnostics = {"cacheHit": True}
        return result
    return None


def _cache_put(source: str, query: str, category: str, region: str, result: SourceResult) -> None:
    if result.status == "ok" and result.items:
        _SOURCE_CACHE[_cache_key(source, query, category, region)] = _clone_result(result)


async def _run_source(source: str, query: str, expanded: list[str], category: str, region: str, limit: int) -> SourceResult:
    started = time.perf_counter()
    try:
        parser = PARSERS[source]()
        result = await asyncio.wait_for(parser.search(query, region=region, limit=limit, category=category), timeout=75)
        if result.status == "empty" and source != "runet":
            for variant in expanded[1:3]:
                result = await asyncio.wait_for(parser.search(variant, region=region, limit=limit, category=category), timeout=45)
                if result.items or result.status == "blocked":
                    break
        latency = int((time.perf_counter() - started) * 1000)
        HEALTH[source] = {
            "source": source,
            "status": result.status,
            "lastError": result.errorReason,
            "lastLatencyMs": latency,
            "lastItemsCount": len(result.items),
        }
        if result.status == "ok" and result.items:
            _cache_put(source, query, category, region, result)
        elif result.status in {"blocked", "error", "empty"}:
            cached = _cache_get(source, query, category, region)
            if cached:
                HEALTH[source]["status"] = "ok"
                HEALTH[source]["lastError"] = "served from cache after source failure"
                HEALTH[source]["lastItemsCount"] = len(cached.items)
                return cached
        return result
    except asyncio.TimeoutError:
        cached = _cache_get(source, query, category, region)
        if cached:
            HEALTH[source] = {"source": source, "status": "ok", "lastError": "served from cache after timeout", "lastLatencyMs": int((time.perf_counter() - started) * 1000), "lastItemsCount": len(cached.items)}
            return cached
        HEALTH[source] = {"source": source, "status": "error", "lastError": "source timeout > 75s", "lastLatencyMs": int((time.perf_counter() - started) * 1000), "lastItemsCount": 0}
        return SourceResult(source, "error", errorReason="source timeout > 75s")
    except Exception as exc:
        cached = _cache_get(source, query, category, region)
        if cached:
            HEALTH[source] = {"source": source, "status": "ok", "lastError": "served from cache after exception", "lastLatencyMs": int((time.perf_counter() - started) * 1000), "lastItemsCount": len(cached.items)}
            return cached
        HEALTH[source] = {"source": source, "status": "error", "lastError": str(exc), "lastLatencyMs": int((time.perf_counter() - started) * 1000), "lastItemsCount": 0}
        return SourceResult(source, "error", errorReason=str(exc))


async def _run_aggregator(query: str, expanded: list[str], category: str, region: str, limit: int) -> SourceResult:
    started = time.perf_counter()
    source = "aggregator"
    try:
        parser = AggregatorParser()
        result = await asyncio.wait_for(
            parser.search(query, region=region, limit=limit, category=category), timeout=25
        )
        # If primary query returns few items, try first expanded variant in parallel
        if len(result.items) < 5 and len(expanded) > 1:
            alt_query = expanded[1]
            try:
                alt = await asyncio.wait_for(
                    parser.search(alt_query, region=region, limit=limit, category=category), timeout=15
                )
                if alt.items:
                    combined = result.items + [i for i in alt.items if i not in result.items]
                    result.items = parser._dedupe(combined)
                    result.count = len(result.items)
                    if result.status == "empty":
                        result.status = "ok"
            except Exception:
                pass
        HEALTH[source] = {
            "source": source,
            "status": result.status,
            "lastError": result.errorReason,
            "lastLatencyMs": int((time.perf_counter() - started) * 1000),
            "lastItemsCount": len(result.items),
        }
        return result
    except Exception as exc:
        HEALTH[source] = {
            "source": source,
            "status": "error",
            "lastError": str(exc),
            "lastLatencyMs": int((time.perf_counter() - started) * 1000),
            "lastItemsCount": 0,
        }
        return SourceResult(source, "error", errorReason=str(exc))


def _postprocess(result: SourceResult, normalized: str, limit: int) -> SourceResult:
    cleaned = []
    for item in result.items:
        item.relevanceScore = calculate_relevance(normalized, item)
        item.completenessScore = calculate_completeness(item)
        item.relevanceDetails = relevance_breakdown(normalized, item)
        if not item.title or not item.url:
            continue
        if item.relevanceScore < 0.03 and len(normalized) >= 3:
            continue
        cleaned.append(item)
    cleaned.sort(key=lambda x: (-x.relevanceScore, -x.completenessScore, x.price or 10**12))
    result.items = cleaned[:limit]
    result.count = len(result.items)
    if result.status == "ok" and not result.items:
        result.status = "empty"
    return result


async def search_products(query: str, category: str, region: str, limit: int = 10) -> dict[str, Any]:
    category = _resolve_category(category)
    # Auto-detect category from query keywords; overrides provided category when
    # signal is unambiguous (e.g. "ноутбук" with category="clothes" → "office").
    detected = detect_category_from_query(query)
    if detected and detected != category:
        category = detected
    normalized = normalize_query(query, category)
    expanded = expand_query(normalized, category)
    limit = max(1, min(int(limit or 10), 30))

    active_sources = CATEGORY_SOURCES.get(category, SOURCE_KEYS)
    tasks = {
        source: asyncio.create_task(_run_source(source, normalized, expanded, category, region, limit))
        for source in active_sources
    }
    aggregator_task = asyncio.create_task(_run_aggregator(normalized, expanded, category, region, limit))
    all_tasks = list(tasks.values()) + [aggregator_task]
    done, pending = await asyncio.wait(all_tasks, timeout=85)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    raw_by_source: dict[str, SourceResult] = {}
    for source in SOURCE_KEYS:
        task = tasks.get(source)
        if not task:
            raw_by_source[source] = SourceResult(
                source,
                "skipped",
                errorReason=f"source is not used for category '{category}'",
                diagnostics={"category": category},
            )
        elif task in done and not task.cancelled():
            value = task.result()
            raw_by_source[source] = value if isinstance(value, SourceResult) else SourceResult(source, "error", errorReason=str(value))
        else:
            raw_by_source[source] = SourceResult(source, "error", errorReason="global timeout > 85s")

    if aggregator_task in done and not aggregator_task.cancelled():
        agg = aggregator_task.result()
        if isinstance(agg, SourceResult) and agg.items:
            for item in agg.items:
                target = item.source if item.source in SOURCE_KEYS else "runet"
                bucket = raw_by_source[target]
                bucket.items.append(item)
                if bucket.status in {"blocked", "error", "empty", "skipped"}:
                    bucket.status = "ok"
                    bucket.errorReason = ""
                bucket.diagnostics.setdefault("aggregatorFallback", "price.ru")
    raw = [raw_by_source[source] for source in SOURCE_KEYS]

    groups = {}
    all_items = []
    for source, result in zip(SOURCE_KEYS, raw):
        result = _postprocess(result, normalized, limit)
        groups[source] = result.to_group()
        all_items.extend(result.items)

    prices = [item.price for item in all_items if item.price]
    return {
        "query": query,
        "normalizedQuery": normalized,
        "expandedQueries": expanded,
        "region": region,
        "category": category,
        "groups": groups,
        "summary": {
            "totalFound": len(all_items),
            "minPrice": min(prices) if prices else 0,
            "maxPrice": max(prices) if prices else 0,
            "sourcesUsed": [source for source, group in groups.items() if group["count"] > 0],
        },
    }


def parsers_health() -> list[dict[str, Any]]:
    return [HEALTH[source] for source in SOURCE_KEYS]
