from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.parsers.aggregator import AggregatorParser
from app.parsers.query_normalizer import SYNONYMS, expand_query, normalize_query
from app.parsers.service import search_products

router = APIRouter()


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=2)
    category: str = "tires"
    region: str = "Москва"
    limit: int = Field(10, ge=1, le=30)


@router.post("/search")
async def search_post(payload: SearchRequest):
    return await search_products(payload.query, payload.category, payload.region, payload.limit)


@router.get("/search")
async def search_get(
    q: str = Query(..., min_length=2),
    category: str = Query("tires"),
    region: str = Query("Москва"),
    limit: int = Query(10, ge=1, le=30),
):
    return await search_products(q, category, region, limit)


@router.get("/search/suggest")
async def suggest(q: str = Query(..., min_length=1)):
    needle = q.lower().strip()
    normalized = normalize_query(needle)
    expanded = expand_query(normalized)[:5]
    local = [value for value in expanded if value and value != needle]
    local.extend([key for key in SYNONYMS if needle in key][:5])

    remote = await AggregatorParser().suggest(normalized or needle, limit=8)
    seen = set()
    suggestions = []
    for value in local + remote.get("suggestions", []):
        text = str(value).strip()
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        suggestions.append(text)
        if len(suggestions) >= 8:
            break

    corrected = normalized if normalized and normalized != needle else ""
    return {
        "query": q,
        "normalizedQuery": normalized,
        "correctedQuery": corrected,
        "suggestions": suggestions,
        "source": remote.get("source", "local"),
        "sourcePolicy": "backend web-flow only; no external API key/sdk",
    }
