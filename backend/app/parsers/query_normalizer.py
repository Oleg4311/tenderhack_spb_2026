import re

RU_LAYOUT = str.maketrans(
    "qwertyuiop[]asdfghjkl;'zxcvbnm,./`",
    "йцукенгшщзхъфывапролджэячсмитьбю.ё",
)

TYPO_FIXES = {
    "кросовки": "кроссовки",
    "арг техника": "оргтехника",
    "орг техника": "оргтехника",
    "принтер лазерный": "лазерный принтер",
    "футболка муж": "мужская футболка",
}

SYNONYMS = {
    "резина": ["шины"],
    "покрышки": ["шины"],
    "колеса": ["шины"],
    "мфу": ["многофункциональное устройство"],
    "оргтехника": ["офисная техника", "орг техника"],
    "лазерный принтер": ["принтер лазерный"],
    "кроссовки": ["кросовки", "sneakers"],
    "мужская футболка": ["футболка муж", "футболка мужская"],
}


def normalize_query(query: str, category: str = "") -> str:
    text = (query or "").lower().strip()
    has_cyrillic = any("а" <= c <= "я" or c == "ё" for c in text)
    # Apply keyboard layout conversion only when text has NO English vowels (a/e/o/u).
    # Genuine English product names (laptop, phone, samsung) have vowels;
    # Russian-typed-with-English-layout produces consonant-heavy strings (htpbyf, ibys).
    # Note: 'i' is excluded from the vowel check because Russian й/ш/и keys map to i in English layout.
    if not has_cyrillic and re.fullmatch(r"[a-z0-9/\- .]+", text):
        pure_letters = re.sub(r"[^a-z]", "", text)
        english_vowels = sum(1 for c in pure_letters if c in "aeou")
        if english_vowels == 0 and pure_letters:
            laid = text.translate(RU_LAYOUT)
            if any("а" <= c <= "я" for c in laid):
                text = laid
    text = re.sub(r"[^\wа-яё/.\- ]+", " ", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip()
    for bad, good in TYPO_FIXES.items():
        text = text.replace(bad, good)
    text = normalize_tire_size(text)
    return text


def normalize_tire_size(text: str) -> str:
    pattern = re.compile(r"\b(\d{3})\s*[/\- ]\s*(\d{2})\s*(?:r|р|/|\-| )?\s*(\d{2})\b", re.I)
    return pattern.sub(lambda m: f"{m.group(1)}/{m.group(2)} R{m.group(3)}", text)


def expand_query(query: str, category: str = "") -> list[str]:
    normalized = normalize_query(query, category)
    variants = [normalized]
    for key, values in SYNONYMS.items():
        if key in normalized:
            for value in values:
                variants.append(normalized.replace(key, value))
        for value in values:
            if value in normalized:
                variants.append(normalized.replace(value, key))

    tire = re.search(r"(\d{3})/(\d{2}) R(\d{2})", normalized, re.I)
    if category == "tires" or tire:
        if tire:
            w, h, r = tire.groups()
            variants.extend([f"шины {w}/{h} R{r}", f"{w} {h} {r} шины", f"{w}-{h}-r{r} покрышки"])
        if "шины" not in normalized:
            variants.append(f"шины {normalized}")

    if category == "clothes":
        variants.extend(_clothes_variants(normalized))
    if category == "office":
        variants.extend(_office_variants(normalized))

    cleaned = []
    for item in variants:
        item = re.sub(r"\s+", " ", item).strip()
        if item and item not in cleaned:
            cleaned.append(item)
    return cleaned[:10]


def _clothes_variants(query: str) -> list[str]:
    colors = "черный белый серый синий красный зеленый бежевый".split()
    sizes = re.findall(r"\b(?:xs|s|m|l|xl|xxl|\d{2})\b", query, re.I)
    found_colors = [c for c in colors if c in query]
    variants = []
    if "муж" in query and "мужская" not in query:
        variants.append(query.replace("муж", "мужская"))
    if found_colors:
        variants.append(query.replace(found_colors[0], "").strip())
    if sizes:
        variants.append(query.replace(sizes[0].lower(), "").strip())
    return variants


def _office_variants(query: str) -> list[str]:
    variants = []
    brands = "canon hp xerox brother epson kyocera pantum".split()
    for brand in brands:
        if brand in query:
            variants.append(query.replace(brand, "").strip())
    if "чб" in query:
        variants.append(query.replace("чб", "черно-белый"))
    if "цветной" in query:
        variants.append(query.replace("цветной", "color"))
    return variants
