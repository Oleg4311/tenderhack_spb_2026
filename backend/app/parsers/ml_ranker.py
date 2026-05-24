"""
ML ranking — scores and sorts ProductItem objects by relevance.
Adapts the logic from ml.py to work with the project's ProductItem dataclass.
"""
from __future__ import annotations
import re
from difflib import SequenceMatcher
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from app.parsers.common import ProductItem

WEIGHT_PRICE = 0.20
WEIGHT_TEXT_SIMILARITY = 0.20
WEIGHT_KEYWORD = 0.40
WEIGHT_COMPLETENESS = 0.20


def _keywords(query: str) -> list[str]:
    return [w for w in re.findall(r"[а-яёa-z0-9]+", query.lower()) if len(w) > 2]


def _price_score(price: float, all_prices: list[float]) -> float:
    if not price or not all_prices:
        return 0.5
    lo, hi = min(all_prices), max(all_prices)
    if lo == hi:
        return 1.0
    return max(0.0, min(1.0, 1.0 - (price - lo) / (hi - lo)))


def _text_sim(item_text: str, query: str) -> float:
    if not item_text or not query:
        return 0.0
    return SequenceMatcher(None, item_text.lower(), query.lower()).ratio()


def _keyword_score(item_text: str, keywords: list[str]) -> float:
    if not keywords or not item_text:
        return 0.0
    text = item_text.lower()
    return sum(1 for kw in keywords if kw in text) / len(keywords)


def _item_searchable_text(item: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("title", "brand", "model", "description", "seller", "availability", "category"):
        v = item.get(key)
        if v:
            parts.append(str(v))
    chars = item.get("characteristics")
    if isinstance(chars, dict):
        parts.extend(str(v) for v in chars.values() if v)
    return " ".join(parts)


def rank_items(all_items: list["ProductItem"], query: str) -> list[dict[str, Any]]:
    """
    Score and sort ProductItem objects by ML relevance.
    Returns serialised item dicts with an added `mlScore` field, sorted descending.
    Items without title or url are excluded.
    """
    from app.parsers.common import ProductItem as _PI  # local import to avoid circulars

    dicts: list[dict[str, Any]] = []
    for item in all_items:
        d = item.to_dict() if isinstance(item, _PI) else dict(item)
        if d.get("title") and d.get("url"):
            dicts.append(d)

    if not dicts:
        return []

    kws = _keywords(query)
    all_prices = [d["price"] for d in dicts if d.get("price")]

    for d in dicts:
        price_s = _price_score(d.get("price", 0), all_prices)
        item_text = _item_searchable_text(d)
        text_s = _text_sim(item_text, query)
        kw_s = _keyword_score(item_text, kws)

        # Blend with backend's own relevanceScore — it's a strong ngram/token signal
        existing = float(d.get("relevanceScore") or d.get("relevance_score") or 0.0)
        blended_text = (text_s + existing) / 2 if existing else text_s

        completeness = float(d.get("completenessScore") or d.get("completeness_score") or 0.0)

        d["mlScore"] = round(
            WEIGHT_PRICE * price_s
            + WEIGHT_TEXT_SIMILARITY * blended_text
            + WEIGHT_KEYWORD * kw_s
            + WEIGHT_COMPLETENESS * completeness,
            4,
        )

    dicts.sort(key=lambda x: x["mlScore"], reverse=True)
    return dicts
