import re
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse


SOURCE_KEYS = ("wildberries", "ozon", "yandex_market", "runet")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ProductItem:
    source: str = ""
    sourceType: str = "marketplace"
    realSourceHost: str = ""
    title: str = ""
    brand: str = ""
    model: str = ""
    sku: str = ""
    productId: str = ""
    category: str = ""
    breadcrumbs: list[str] = field(default_factory=list)
    price: float = 0
    oldPrice: float = 0
    discountPercent: float = 0
    currency: str = "RUB"
    availability: str = ""
    seller: str = ""
    rating: float = 0
    reviewsCount: int = 0
    images: list[str] = field(default_factory=list)
    mainImage: str = ""
    url: str = ""
    characteristics: dict[str, Any] = field(default_factory=dict)
    description: str = ""
    deliveryInfo: str = ""
    region: str = ""
    geo: dict[str, Any] = field(default_factory=dict)
    relevanceScore: float = 0
    relevanceDetails: dict[str, Any] = field(default_factory=dict)
    completenessScore: float = 0
    collectedAt: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        if not self.geo:
            self.geo = default_geo(self.region)
        if self.mainImage and self.mainImage not in self.images:
            self.images.insert(0, self.mainImage)
        if not self.mainImage and self.images:
            self.mainImage = self.images[0]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        # Compatibility for older frontend code that may still read snake_case.
        data["old_price"] = self.oldPrice
        data["image_url"] = self.mainImage
        data["product_url"] = self.url
        data["reviews_count"] = self.reviewsCount
        data["relevance_score"] = self.relevanceScore
        data["completeness_score"] = self.completenessScore
        data["domain"] = self.realSourceHost
        data["id"] = self.productId or self.sku
        return data


@dataclass
class SourceResult:
    source: str
    status: str = "empty"
    count: int = 0
    errorReason: str = ""
    items: list[ProductItem] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_group(self) -> dict[str, Any]:
        self.count = len(self.items)
        if self.status == "empty" and self.items:
            self.status = "ok"
        return {
            "status": self.status,
            "count": self.count,
            "errorReason": self.errorReason,
            "diagnostics": self.diagnostics,
            "items": [item.to_dict() for item in self.items],
        }


def default_geo(region: str = "") -> dict[str, Any]:
    return {
        "requestedRegion": region or "",
        "detectedRegion": "",
        "city": region or "",
        "deliveryRegion": region or "",
        "storeAddress": "",
        "pickupAddress": "",
        "warehouse": "",
        "latitude": None,
        "longitude": None,
    }


def clean_text(value: Any) -> str:
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()


def flatten_value(value: Any, *, max_items: int = 120) -> str:
    parts: list[str] = []

    def walk(node: Any, depth: int = 0) -> None:
        if len(parts) >= max_items or depth > 5:
            return
        if isinstance(node, dict):
            for key, val in node.items():
                if val in ("", None, [], {}):
                    continue
                parts.append(clean_text(key))
                walk(val, depth + 1)
        elif isinstance(node, list):
            for item in node[:max_items]:
                walk(item, depth + 1)
        else:
            text = clean_text(node)
            if text:
                parts.append(text)

    walk(value)
    return " ".join(parts)


def product_relevance_text(item: "ProductItem") -> str:
    fields = [
        item.title,
        item.brand,
        item.model,
        item.sku,
        item.productId,
        item.category,
        " ".join(item.breadcrumbs),
        item.availability,
        item.seller,
        item.description,
        item.deliveryInfo,
        flatten_value(item.characteristics),
    ]
    return clean_text(" ".join(str(x) for x in fields if x))


def calculate_completeness(item: "ProductItem") -> float:
    checks = [
        bool(item.title),
        bool(item.price),
        bool(item.url),
        bool(item.mainImage or item.images),
        bool(item.characteristics),
        len(item.characteristics or {}) >= 3,
        bool(item.description),
        bool(item.brand or item.model),
        bool(item.seller),
        bool(item.availability or item.deliveryInfo),
        bool(item.geo and (item.geo.get("detectedRegion") or item.geo.get("deliveryRegion") or item.geo.get("city"))),
        bool(item.rating or item.reviewsCount),
    ]
    return round(sum(1 for item_ok in checks if item_ok) / len(checks), 4)


def normalize_price(value: Any) -> float:
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        val = float(value)
        if val > 10_000_000 and val % 100 == 0:
            val /= 100
        return val if 1 <= val <= 100_000_000 else 0
    text = str(value).replace("\xa0", " ")
    match = re.search(r"(\d[\d\s.,]{1,14})", text)
    if not match:
        return 0
    number = re.sub(r"[^\d]", "", match.group(1))
    if not number:
        return 0
    val = float(number)
    if val > 10_000_000 and val % 100 == 0:
        val /= 100
    return val if 1 <= val <= 100_000_000 else 0


def normalize_url(url: str, base_url: str = "") -> str:
    if not url:
        return ""
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url
    if base_url:
        url = urljoin(base_url, url)
    parsed = urlparse(url)
    if not parsed.scheme:
        return url
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", parsed.query, ""))


def _tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-zа-яё0-9]+(?:/[a-zа-яё0-9]+)?", (text or "").lower()) if len(t) > 1]


def _char_ngrams(text: str, n: int = 3) -> dict[str, int]:
    compact = re.sub(r"\s+", " ", (text or "").lower())
    if len(compact) < n:
        return {compact: 1} if compact else {}
    out: dict[str, int] = {}
    for idx in range(len(compact) - n + 1):
        gram = compact[idx:idx + n]
        out[gram] = out.get(gram, 0) + 1
    return out


def _cosine(left: dict[str, int], right: dict[str, int]) -> float:
    if not left or not right:
        return 0.0
    dot = sum(value * right.get(key, 0) for key, value in left.items())
    if not dot:
        return 0.0
    l_norm = math.sqrt(sum(v * v for v in left.values()))
    r_norm = math.sqrt(sum(v * v for v in right.values()))
    return dot / (l_norm * r_norm) if l_norm and r_norm else 0.0


def calculate_relevance(query: str, item_or_title: Any, characteristics: dict[str, Any] | None = None) -> float:
    if isinstance(item_or_title, ProductItem):
        haystack = product_relevance_text(item_or_title)
        title = item_or_title.title
    else:
        title = str(item_or_title or "")
        haystack = f"{title} {flatten_value(characteristics or {})}"

    query_text = clean_text(query).lower()
    haystack_text = clean_text(haystack).lower()
    if not query_text or not haystack_text:
        return 0.0

    q_tokens = _tokens(query_text)
    h_tokens = _tokens(haystack_text)
    h_set = set(h_tokens)
    if not q_tokens:
        return 0.0

    # Use token-set membership only — substring check (token in text) causes false
    # positives: "мяч" would match inside "хомачки", "включая" inside "ключ", etc.
    exact_hits = sum(1 for token in q_tokens if token in h_set)
    token_score = exact_hits / len(q_tokens)
    title_tokens = set(_tokens(title))
    title_score = sum(1 for token in q_tokens if token in title_tokens) / len(q_tokens)
    ngram_score = _cosine(_char_ngrams(query_text), _char_ngrams(haystack_text))

    numbers = re.findall(r"\d+(?:/\d+)?", query_text)
    number_score = 1.0 if numbers and all(number in haystack_text for number in numbers) else 0.0
    phrase_bonus = 0.15 if query_text in haystack_text else 0.0

    score = token_score * 0.42 + title_score * 0.22 + ngram_score * 0.24 + number_score * 0.12 + phrase_bonus
    return round(min(score, 1.0), 4)


def relevance_breakdown(query: str, item: "ProductItem") -> dict[str, Any]:
    text = product_relevance_text(item).lower()
    q_tokens = _tokens(query)
    h_set = set(_tokens(text))
    matched = [token for token in q_tokens if token in h_set]
    missing = [token for token in q_tokens if token not in h_set]
    return {
        "matchedTokens": matched,
        "missingTokens": missing,
        "characteristicsUsed": len(item.characteristics or {}),
        "textFieldsUsed": [
            key for key, value in {
                "title": item.title,
                "brand": item.brand,
                "model": item.model,
                "description": item.description,
                "seller": item.seller,
                "breadcrumbs": item.breadcrumbs,
                "characteristics": item.characteristics,
            }.items() if value
        ],
    }


def detect_blocked_page(html: str = "", status_code: int = 200) -> bool:
    if status_code in {401, 403, 407, 418, 429, 451, 503}:
        return True
    text = (html or "").lower()
    markers = (
        "captcha", "капча", "доступ ограничен", "access denied", "forbidden",
        "too many requests", "robot check", "antibot", "подтвердите, что вы не робот",
        "enable javascript", "vpn", "challenge",
    )
    return any(marker in text for marker in markers)


def merge_product_data(summary: ProductItem, details: ProductItem | dict[str, Any] | None) -> ProductItem:
    if not details:
        return summary
    detail_data = details.to_dict() if isinstance(details, ProductItem) else details
    for field_name in ProductItem.__dataclass_fields__:
        current = getattr(summary, field_name)
        value = detail_data.get(field_name)
        if field_name == "characteristics" and isinstance(value, dict):
            current.update({k: v for k, v in value.items() if v not in ("", None, [], {})})
        elif field_name in {"images", "breadcrumbs"} and isinstance(value, list):
            merged = list(dict.fromkeys([*current, *value]))
            setattr(summary, field_name, merged)
        elif value not in ("", None, 0, [], {}):
            setattr(summary, field_name, value)
    summary.__post_init__()
    return summary
