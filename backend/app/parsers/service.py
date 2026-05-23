import asyncio
import time
from typing import Any

from app.parsers.common import SOURCE_KEYS, SourceResult, calculate_completeness, calculate_relevance, relevance_breakdown
from app.parsers.ozon import OzonParser
from app.parsers.query_normalizer import expand_query, normalize_query
from app.parsers.runet import RunetParser
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


async def _run_source(source: str, query: str, expanded: list[str], category: str, region: str, limit: int) -> SourceResult:
    started = time.perf_counter()
    try:
        parser = PARSERS[source]()
        result = await asyncio.wait_for(parser.search(query, region=region, limit=limit, category=category), timeout=28)
        

        # ── SearxNG fallback при блокировке ──
        if result.status == "blocked" and source != "runet":
            try:
                from app.parsers.service_patch import searxng_fallback
                fallback_result = await searxng_fallback(source, query, region, limit, category)
                if fallback_result and fallback_result.items:
                    result = fallback_result
                    result.diagnostics["recovery"] = "searxng_fallback"
            except Exception as exc:
                logger.warning(f"[{source}] SearxNG fallback failed: {exc}")



        if result.status == "empty" and source != "runet":
            for variant in expanded[1:3]:
                result = await asyncio.wait_for(parser.search(variant, region=region, limit=limit, category=category), timeout=20)
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
        return result
    except asyncio.TimeoutError:
        HEALTH[source] = {"source": source, "status": "error", "lastError": "source timeout > 20s", "lastLatencyMs": int((time.perf_counter() - started) * 1000), "lastItemsCount": 0}
        return SourceResult(source, "error", errorReason="source timeout > 20s")
    except Exception as exc:
        HEALTH[source] = {"source": source, "status": "error", "lastError": str(exc), "lastLatencyMs": int((time.perf_counter() - started) * 1000), "lastItemsCount": 0}
        return SourceResult(source, "error", errorReason=str(exc))


def _postprocess(result: SourceResult, normalized: str, limit: int) -> SourceResult:
    cleaned = []
    for item in result.items:
        item.relevanceScore = calculate_relevance(normalized, item)
        item.completenessScore = calculate_completeness(item)
        item.relevanceDetails = relevance_breakdown(normalized, item)
        if not item.title or not item.url:
            continue
        if item.relevanceScore < 0.03 and len(normalized) > 3:
            continue
        cleaned.append(item)
    cleaned.sort(key=lambda x: (-x.relevanceScore, -x.completenessScore, x.price or 10**12))
    result.items = cleaned[:limit]
    result.count = len(result.items)
    if result.status == "ok" and not result.items:
        result.status = "empty"
    return result


async def search_products(query: str, category: str, region: str, limit: int = 10) -> dict[str, Any]:
    normalized = normalize_query(query, category)
    expanded = expand_query(normalized, category)
    limit = max(1, min(int(limit or 10), 30))

    tasks = {
        source: asyncio.create_task(_run_source(source, normalized, expanded, category, region, limit))
        for source in SOURCE_KEYS
    }
    done, pending = await asyncio.wait(tasks.values(), timeout=38)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    raw_by_source: dict[str, SourceResult] = {}
    for source, task in tasks.items():
        if task in done and not task.cancelled():
            value = task.result()
            raw_by_source[source] = value if isinstance(value, SourceResult) else SourceResult(source, "error", errorReason=str(value))
        else:
            raw_by_source[source] = SourceResult(source, "error", errorReason="global timeout > 30s")
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
