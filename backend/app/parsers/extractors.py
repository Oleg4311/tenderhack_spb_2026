import json
import re
from typing import Any
from urllib.parse import urlparse

try:
    from bs4 import BeautifulSoup
except ModuleNotFoundError:
    BeautifulSoup = None

from app.parsers.common import ProductItem, clean_text, default_geo, normalize_price, normalize_url


def _soup(html: str):
    if BeautifulSoup is None:
        return None
    try:
        return BeautifulSoup(html or "", "html.parser")
    except Exception:
        return BeautifulSoup(html or "", "html.parser")


def _loads(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return None


def _walk(node: Any, depth: int = 0):
    if depth > 12:
        return
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value, depth + 1)
    elif isinstance(node, list):
        for item in node[:300]:
            yield from _walk(item, depth + 1)
    elif isinstance(node, str) and 5 < len(node) < 250_000 and node.lstrip()[:1] in "{[":
        parsed = _loads(node)
        if parsed is not None:
            yield from _walk(parsed, depth + 1)


def extract_jsonld_products(html: str) -> list[dict[str, Any]]:
    if BeautifulSoup is None:
        out = []
        for match in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html or "", re.S | re.I):
            data = _loads(match.group(1))
            for node in _walk(data):
                typ = node.get("@type") if isinstance(node, dict) else None
                types = typ if isinstance(typ, list) else [typ]
                if any(str(t).lower() in {"product", "offer"} for t in types):
                    out.append(node)
        return out
    soup = _soup(html)
    out: list[dict[str, Any]] = []
    for tag in soup.find_all("script", type="application/ld+json"):
        data = _loads(tag.string or tag.get_text() or "")
        for node in _walk(data):
            typ = node.get("@type") if isinstance(node, dict) else None
            types = typ if isinstance(typ, list) else [typ]
            if any(str(t).lower() in {"product", "offer"} for t in types):
                out.append(node)
    return out


def extract_microdata(html: str) -> list[dict[str, Any]]:
    if BeautifulSoup is None:
        return []
    soup = _soup(html)
    products = []
    for scope in soup.select('[itemscope][itemtype*="Product"]'):
        data: dict[str, Any] = {}
        for prop in scope.select("[itemprop]"):
            key = prop.get("itemprop")
            if not key:
                continue
            content = prop.get("content")
            # Some CMSes (Bitrix) embed internal IDs in content for price — prefer visible text when > 100k
            if key == "price" and content:
                try:
                    if float(content) > 100_000:
                        content = None
                except (ValueError, TypeError):
                    pass
            data[key] = content or prop.get("src") or prop.get("href") or prop.get_text(" ", strip=True)
        products.append(data)
    return products


def extract_next_data(html: str) -> Any:
    match = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', html or "", re.S)
    return _loads(match.group(1)) if match else None


def extract_initial_state(html: str) -> list[Any]:
    out = []
    patterns = [
        r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*;",
        r"window\.__PRELOADED_STATE__\s*=\s*(\{.*?\})\s*;",
        r"window\.__NUXT__\s*=\s*(\{.*?\})\s*;",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, html or "", re.S):
            data = _loads(match.group(1))
            if data is not None:
                out.append(data)
    return out


def extract_embedded_json(html: str) -> list[Any]:
    if BeautifulSoup is None:
        out: list[Any] = []
        next_data = extract_next_data(html)
        if next_data is not None:
            out.append(next_data)
        out.extend(extract_initial_state(html))
        for match in re.finditer(r'<script[^>]+type=["\'][^"\']*json[^"\']*["\'][^>]*>(.*?)</script>', html or "", re.S | re.I):
            data = _loads(match.group(1))
            if data is not None:
                out.append(data)
        return out
    soup = _soup(html)
    out: list[Any] = []
    next_data = extract_next_data(html)
    if next_data is not None:
        out.append(next_data)
    out.extend(extract_initial_state(html))
    for tag in soup.find_all("script", type=re.compile("json", re.I)):
        data = _loads(tag.string or tag.get_text() or "")
        if data is not None:
            out.append(data)
    return out


_PRODUCT_NAME_KEYS = {"name", "title", "goodsName", "productName", "displayName"}
_PRODUCT_PRICE_KEYS = {"price", "salePrice", "finalPrice", "priceU", "currentPrice", "cardPrice", "priceValue"}
_PRODUCT_ID_KEYS = {"id", "sku", "nmId", "productId", "skuId", "wareId", "articleId"}
_PRODUCT_URL_KEYS = {"url", "link", "productUrl", "href", "detailUrl"}


def looks_like_product(obj: dict) -> bool:
    keys = set(obj.keys())
    has_name = bool(keys & _PRODUCT_NAME_KEYS)
    has_price = bool(keys & _PRODUCT_PRICE_KEYS)
    has_id = bool(keys & _PRODUCT_ID_KEYS)
    has_url = bool(keys & _PRODUCT_URL_KEYS)
    return has_name and (has_price or has_id or has_url)


def find_products_in_json(data: Any, _depth: int = 0, _max: int = 14) -> list[dict]:
    if _depth > _max:
        return []
    results: list[dict] = []
    if isinstance(data, dict):
        if looks_like_product(data):
            results.append(data)
        else:
            for v in data.values():
                results.extend(find_products_in_json(v, _depth + 1, _max))
    elif isinstance(data, list):
        for item in data[:500]:
            results.extend(find_products_in_json(item, _depth + 1, _max))
    return results


def _card_to_dict(card, base_url: str = "") -> dict:
    """Extract product data from one DOM card element by semantic patterns."""
    # Title: first meaningful heading or labeled text node
    title = ""
    for sel in ["h3", "h2", "h1", "[class*='name' i]", "[class*='title' i]", "[class*='label' i]", "strong", "a[href]"]:
        try:
            el = card.select_one(sel)
        except Exception:
            continue
        if el:
            text = el.get_text(" ", strip=True)
            if 5 < len(text) < 300:
                title = text
                break
    if not title:
        title = card.get("data-name") or card.get("title") or card.get("aria-label") or ""

    # Price: prefer data-attributes, then itemprop, then class-based
    price = 0.0
    for sel in ['[itemprop="price"]', "[data-price]", "[class*='price' i]", "[data-auto*='price' i]", "[class*='cost' i]"]:
        try:
            els = card.select(sel)
        except Exception:
            continue
        for el in els:
            raw = el.get("content") or el.get("data-price") or el.get_text(" ", strip=True)
            p = normalize_price(raw)
            if 10 < p < 50_000_000:
                price = p
                break
        if price:
            break
    if not price:
        price = normalize_price(card.get_text(" ", strip=True))

    # URL: first meaningful link
    url = ""
    for a in card.select("a[href]"):
        href = str(a.get("href", ""))
        if href and href != "#" and not href.startswith("javascript") and len(href) > 3:
            url = normalize_url(href, base_url)
            break

    # Image: prefer data-src (lazy) over src
    image = ""
    for el in card.select("img"):
        src = el.get("data-src") or el.get("data-original") or el.get("src") or ""
        if src and not src.startswith("data:") and len(src) > 10:
            image = normalize_url(src, base_url)
            break

    # Rating
    rating = 0.0
    for sel in ["[class*='rating' i]", "[class*='stars' i]", "[data-auto*='rating' i]", "[class*='review' i]"]:
        try:
            el = card.select_one(sel)
        except Exception:
            continue
        if el:
            m = re.search(r"([1-5][.,]\d)", el.get_text(" ", strip=True))
            if m:
                rating = float(m.group(1).replace(",", "."))
                break

    product_id = (
        card.get("data-nm-id") or card.get("data-id") or
        card.get("data-product-id") or card.get("data-sku") or ""
    )
    brand = card.get("data-brand") or ""

    return {
        "title": clean_text(title),
        "price": price,
        "url": url,
        "image": image,
        "rating": rating,
        "brand": clean_text(brand),
        "product_id": str(product_id),
    }


def extract_dom_cards(html: str, base_url: str = "") -> list[dict]:
    """Extract product cards using multiple selector patterns (not a single CSS class).

    Tries selectors from most specific to most generic. Stops at the first one
    that produces at least 2 cards with a title + (price or url).
    """
    if BeautifulSoup is None:
        return []
    soup = _soup(html)
    # Ordered by specificity: WB → YM → Ozon → generic article → class patterns
    CARD_SELECTORS = [
        'article[data-nm-id]',           # WB product card
        '[data-zone-name="snippet"]',     # YM snippet
        '[data-auto="snippet"]',
        '[data-testid*="product"]',
        '[data-auto*="product"]',
        'article[class*="tile"]',
        'article[class*="product"]',
        'article[class*="card"]',
        'article',
        '[class*="product-card"]',
        '[class*="ProductCard"]',
        '[class*="product_card"]',
        '[class*="goods-tile"]',
        '[class*="tile-root"]',
        '[class*="tileContent"]',
        '[class*="search-snippet"]',
        '[class*="searchSnippet"]',
        'li[class*="product"]',
        'div[class*="product"][data-id]',
        'div[class*="item"][data-id]',
    ]
    for selector in CARD_SELECTORS:
        try:
            found = soup.select(selector)
        except Exception:
            continue
        if len(found) < 2:
            continue
        cards = []
        for el in found[:150]:
            d = _card_to_dict(el, base_url)
            if d["title"] and (d["price"] or d["url"]):
                cards.append(d)
        if len(cards) >= 2:
            return cards
    return []


def _add_char(chars: dict[str, Any], key: Any, value: Any) -> None:
    key_text = clean_text(key)
    if not key_text or len(key_text) > 80:
        return
    if key_text.lower() in {"url", "href", "link", "image", "src", "picture", "thumbnail"}:
        return
    if isinstance(value, (dict, list)):
        value_text = clean_text(json.dumps(value, ensure_ascii=False))[:500]
    else:
        value_text = clean_text(value)
    if not value_text or value_text == key_text or len(value_text) > 800:
        return
    chars.setdefault(key_text, value_text)


def extract_characteristics_from_json(data: Any, limit: int = 80) -> dict[str, Any]:
    chars: dict[str, Any] = {}
    spec_keys = {
        "characteristics", "attributes", "properties", "params", "specs", "specifications",
        "features", "options", "techSpecs", "filters", "details", "shortCharacteristics",
    }
    name_keys = {"name", "title", "label", "key", "property", "parameter", "displayName"}
    value_keys = {"value", "values", "text", "description", "content", "displayValue"}

    def walk(node: Any, depth: int = 0, force: bool = False) -> None:
        if len(chars) >= limit or depth > 12:
            return
        if isinstance(node, dict):
            name = next((node.get(k) for k in name_keys if node.get(k)), None)
            value = next((node.get(k) for k in value_keys if node.get(k)), None)
            if name and value:
                _add_char(chars, name, value)

            for key, val in node.items():
                key_str = str(key)
                next_force = force or key_str in spec_keys or "характер" in key_str.lower() or "spec" in key_str.lower()
                if next_force and not isinstance(val, (dict, list)):
                    _add_char(chars, key, val)
                walk(val, depth + 1, next_force)
        elif isinstance(node, list):
            for item in node[:300]:
                walk(item, depth + 1, force)

    walk(data)
    return chars


def extract_geo_from_json(data: Any) -> dict[str, Any]:
    geo = default_geo("")

    def set_text(key: str, value: Any) -> None:
        text = clean_text(value)
        if text and not geo.get(key):
            geo[key] = text

    for node in _walk(data):
        if not isinstance(node, dict):
            continue
        address = node.get("address")
        if isinstance(address, dict):
            address_text = clean_text(", ".join(str(x) for x in [
                address.get("addressLocality"),
                address.get("streetAddress"),
                address.get("postalCode"),
            ] if x))
            set_text("storeAddress", address_text)
            set_text("pickupAddress", address_text)
            set_text("city", address.get("addressLocality"))
            set_text("detectedRegion", address.get("addressRegion") or address.get("addressLocality"))
        elif address:
            set_text("storeAddress", address)
            set_text("pickupAddress", address)

        geo_node = node.get("geo") if isinstance(node.get("geo"), dict) else node
        lat = geo_node.get("latitude") or geo_node.get("lat")
        lon = geo_node.get("longitude") or geo_node.get("lng") or geo_node.get("lon")
        try:
            if lat is not None and lon is not None:
                geo["latitude"] = float(str(lat).replace(",", "."))
                geo["longitude"] = float(str(lon).replace(",", "."))
        except Exception:
            pass

        for source_key, target_key in (
            ("city", "city"),
            ("town", "city"),
            ("addressLocality", "city"),
            ("region", "deliveryRegion"),
            ("deliveryRegion", "deliveryRegion"),
        ):
            if node.get(source_key):
                set_text(target_key, node.get(source_key))
    return geo


def extract_product_links(html: str, base_url: str) -> list[str]:
    if BeautifulSoup is None:
        links = []
        for href in re.findall(r'<a[^>]+href=["\']([^"\']+)["\']', html or "", re.I):
            if any(part in href.lower() for part in ("/product", "/catalog", "/item", "/tovar", "/goods", "/shop/")):
                url = normalize_url(href, base_url)
                if url and url not in links:
                    links.append(url)
        return links[:80]
    soup = _soup(html)
    links: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(" ", strip=True).lower()
        if any(part in href.lower() for part in ("/product", "/catalog", "/item", "/tovar", "/goods", "/shop/")) or len(text) > 20:
            url = normalize_url(href, base_url)
            host = urlparse(url).netloc
            if host and url not in links:
                links.append(url)
    return links[:80]


def extract_images(html: str, base_url: str) -> list[str]:
    if BeautifulSoup is None:
        images = []
        patterns = [
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
            r'<img[^>]+(?:src|data-src|data-original)=["\']([^"\']+)["\']',
        ]
        for pattern in patterns:
            for src in re.findall(pattern, html or "", re.I):
                url = normalize_url(src, base_url)
                if url and url not in images and not url.startswith("data:"):
                    images.append(url)
        return images[:12]
    soup = _soup(html)
    images = []
    for tag in soup.select('meta[property="og:image"], img'):
        src = tag.get("content") or tag.get("src") or tag.get("data-src") or tag.get("data-original")
        url = normalize_url(src or "", base_url)
        if url and url not in images and not url.startswith("data:"):
            images.append(url)
    return images[:12]


def extract_characteristics(html: str) -> dict[str, str]:
    if BeautifulSoup is None:
        chars = {}
        text = clean_text(html[:120_000])
        for key, value in re.findall(r"([А-Яа-яA-Za-z][^:]{2,50}):\s*([^:]{2,80})", text):
            chars.setdefault(clean_text(key), clean_text(value))
            if len(chars) >= 20:
                break
        return chars
    soup = _soup(html)
    chars: dict[str, str] = {}
    for data in extract_jsonld_products(html) + extract_microdata(html) + extract_embedded_json(html):
        chars.update({k: v for k, v in extract_characteristics_from_json(data, limit=50).items() if k not in chars})
        if len(chars) >= 80:
            return chars
    for row in soup.select("tr, li, dl, div"):
        text = clean_text(row.get_text(" ", strip=True))
        if 4 <= len(text) <= 160 and (":" in text or " - " in text):
            if ":" in text:
                key, value = text.split(":", 1)
            else:
                key, value = text.split(" - ", 1)
            key, value = clean_text(key), clean_text(value)
            if 1 < len(key) < 60 and value and key.lower() not in {"цена", "купить"}:
                chars.setdefault(key, value)
        if len(chars) >= 30:
            break
    return chars


def extract_breadcrumbs(html: str) -> list[str]:
    if BeautifulSoup is None:
        return []
    soup = _soup(html)
    crumbs = [clean_text(x.get_text(" ", strip=True)) for x in soup.select('[itemprop="itemListElement"], nav a, .breadcrumb a')]
    return [c for c in crumbs if c][:12]


def extract_price(html: str) -> float:
    if BeautifulSoup is None:
        return normalize_price(html[:120_000])
    soup = _soup(html)
    for selector in ('[itemprop="price"]', 'meta[property="product:price:amount"]', '[class*="price" i]'):
        for tag in soup.select(selector):
            content_val = tag.get("content") or ""
            content_price = normalize_price(content_val) if content_val else 0
            text_price = normalize_price(tag.get_text(" ", strip=True))
            # Skip suspiciously large content values (Bitrix-style internal IDs in kopeks)
            if content_price and content_price < 100_000:
                return content_price
            if text_price and text_price < 100_000:
                return text_price
            if content_price:
                return content_price
    return normalize_price(html[:120_000])


def extract_rating(html: str) -> tuple[float, int]:
    text = clean_text(_soup(html).get_text(" ", strip=True)) if BeautifulSoup is not None else clean_text(html)
    rating = 0.0
    reviews = 0
    m = re.search(r"([1-5][.,]\d)", text)
    if m:
        rating = float(m.group(1).replace(",", "."))
    r = re.search(r"(\d[\d\s]*)\s*(?:отзыв|оцен)", text, re.I)
    if r:
        reviews = int(re.sub(r"\D", "", r.group(1)))
    return rating, reviews


def extract_description(html: str) -> str:
    if BeautifulSoup is None:
        match = re.search(r'<meta[^>]+(?:name|property)=["\'](?:description|og:description)["\'][^>]+content=["\']([^"\']+)["\']', html or "", re.I)
        return clean_text(match.group(1))[:2000] if match else ""
    soup = _soup(html)
    meta = soup.select_one('meta[name="description"], meta[property="og:description"]')
    if meta:
        return clean_text(meta.get("content"))[:2000]
    for selector in ('[itemprop="description"]', '[class*="description" i]'):
        node = soup.select_one(selector)
        if node:
            return clean_text(node.get_text(" ", strip=True))[:2000]
    return ""


def extract_geo(html: str) -> dict[str, Any]:
    text = clean_text(_soup(html).get_text(" ", strip=True)) if BeautifulSoup is not None else clean_text(html)
    geo = default_geo("")
    for data in extract_jsonld_products(html) + extract_microdata(html) + extract_embedded_json(html):
        json_geo = extract_geo_from_json(data)
        for key, value in json_geo.items():
            if value not in ("", None) and not geo.get(key):
                geo[key] = value
    city = re.search(r"(Москва|Санкт-Петербург|Новосибирск|Екатеринбург|Казань|Краснодар)", text, re.I)
    if city:
        geo["detectedRegion"] = city.group(1)
        geo["city"] = city.group(1)
    address = re.search(r"(?:адрес|самовывоз|пункт выдачи)[:\s]+(.{10,120})", text, re.I)
    if address:
        geo["pickupAddress"] = clean_text(address.group(1))
    coords = re.search(r"(?<!\d)([45]\d\.\d{3,}|6[0-9]\.\d{3,})[,;\s]+([3-5]\d\.\d{3,})", text)
    if coords:
        try:
            geo["latitude"] = float(coords.group(1))
            geo["longitude"] = float(coords.group(2))
        except Exception:
            pass
    return geo


def _product_from_mapping(data: dict[str, Any], url: str, source: str) -> ProductItem:
    offers = data.get("offers") if isinstance(data.get("offers"), dict) else {}
    aggregate = data.get("aggregateRating") if isinstance(data.get("aggregateRating"), dict) else {}
    image = data.get("image") or data.get("images")
    images = image if isinstance(image, list) else [image] if image else []
    title = data.get("name") or data.get("title") or data.get("model") or ""
    price = normalize_price(offers.get("price") or data.get("price") or data.get("lowPrice"))
    return ProductItem(
        source=source,
        sourceType="runet" if source == "runet" else "marketplace",
        realSourceHost=urlparse(url).netloc,
        title=clean_text(title),
        brand=clean_text(data.get("brand", {}).get("name") if isinstance(data.get("brand"), dict) else data.get("brand")),
        model=clean_text(data.get("model")),
        sku=clean_text(data.get("sku")),
        productId=clean_text(data.get("productID") or data.get("id") or data.get("sku")),
        price=price,
        oldPrice=normalize_price(data.get("oldPrice") or data.get("basePrice")),
        availability=clean_text(offers.get("availability") or data.get("availability")),
        seller=clean_text((offers.get("seller") or {}).get("name") if isinstance(offers.get("seller"), dict) else data.get("seller")),
        rating=float(aggregate.get("ratingValue") or data.get("ratingValue") or 0),
        reviewsCount=int(float(aggregate.get("reviewCount") or data.get("reviewCount") or 0)),
        images=[normalize_url(str(i), url) for i in images if i],
        url=normalize_url(data.get("url") or url, url),
        description=clean_text(data.get("description"))[:2000],
        geo=extract_geo_from_json(data),
        characteristics=extract_characteristics_from_json(data, limit=80),
    )


def extract_product_from_html(html: str, url: str, source: str) -> ProductItem:
    for data in extract_jsonld_products(html) + extract_microdata(html):
        item = _product_from_mapping(data, url, source)
        if item.title or item.price:
            break
    else:
        if BeautifulSoup is not None:
            soup = _soup(html)
            h1 = soup.find("h1")
            title = h1.get_text(" ", strip=True) if h1 else ""
        else:
            match = re.search(r"<h1[^>]*>(.*?)</h1>", html or "", re.S | re.I)
            title = clean_text(match.group(1)) if match else ""
        item = ProductItem(source=source, sourceType="runet" if source == "runet" else "marketplace", realSourceHost=urlparse(url).netloc, title=clean_text(title), url=url)
    item.url = item.url or url
    item.images = list(dict.fromkeys(item.images + extract_images(html, url)))
    item.mainImage = item.mainImage or (item.images[0] if item.images else "")
    item.price = item.price or extract_price(html)
    rating, reviews = extract_rating(html)
    item.rating = item.rating or rating
    item.reviewsCount = item.reviewsCount or reviews
    item.characteristics.update(extract_characteristics(html))
    item.description = item.description or extract_description(html)
    item.breadcrumbs = item.breadcrumbs or extract_breadcrumbs(html)
    item.geo.update({k: v for k, v in extract_geo(html).items() if v})
    item.__post_init__()
    return item
