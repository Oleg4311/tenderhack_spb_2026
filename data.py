import json
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Типы и константы
# ---------------------------------------------------------------------------

Item = Dict[str, Any]

# Синонимы полей: приводим разные названия к единому внутреннему ключу.
# Добавляй сюда новые алиасы по мере появления новых источников.
FIELD_ALIASES: Dict[str, List[str]] = {
    "цена": [
        "цена", "price", "стоимость", "цена, руб", "цена руб",
        "розничная цена", "цена со скидкой",
    ],
    "мощность": [
        "мощность", "мощность (вт)", "мощность(вт)", "power", "power (w)",
        "мощность вт", "watt",
    ],
    "объём": [
        "объём", "объем", "объём (л)", "объем (л)", "volume",
        "ёмкость", "емкость", "литраж",
    ],
    "рейтинг": [
        "рейтинг", "rating", "оценка", "звёзды", "звезды", "score",
    ],
    "отзывы": [
        "отзывы", "количество отзывов", "reviews", "число отзывов",
        "кол-во отзывов",
    ],
    "бренд": [
        "бренд", "brand", "производитель", "марка",
    ],
    "модель": [
        "модель", "model", "наименование", "название",
    ],
    "материал": [
        "материал", "material", "корпус",
    ],
    "цвет": [
        "цвет", "color", "colour", "расцветка",
    ],
    "тип": [
        "тип", "type", "вид", "категория",
    ],
}

# Веса для финального скора. Сумма не обязана быть 1 — нормализуем сами.
# Смысл: насколько этот фактор важен для пользователя.
SCORE_WEIGHTS: Dict[str, float] = {
    "цена":    3.0,   # самый важный фактор
    "рейтинг": 2.0,   # если есть — очень важно
    "отзывы":  1.5,   # популярность
    "мощность": 1.0,  # характеристика товара
    "объём":   1.0,   # характеристика товара
}

# Для каких полей «больше = лучше», для каких «меньше = лучше»
HIGHER_IS_BETTER: Dict[str, bool] = {
    "цена":    False,  # дешевле — лучше
    "рейтинг": True,
    "отзывы":  True,
    "мощность": True,
    "объём":   True,
}


# ---------------------------------------------------------------------------
# Нормализация полей
# ---------------------------------------------------------------------------

def build_alias_map() -> Dict[str, str]:
    """
    Строит обратный словарь: любое написание поля -> канонический ключ.
    
    Например:
        "мощность (вт)" -> "мощность"
        "power"         -> "мощность"
    """
    alias_map: Dict[str, str] = {}

    for canonical, aliases in FIELD_ALIASES.items():
        for alias in aliases:
            alias_map[alias.lower().strip()] = canonical

    return alias_map


ALIAS_MAP = build_alias_map()


def normalize_item(item: Item) -> Item:
    """
    Приводит поля словаря к каноническим именам.
    
    Неизвестные поля сохраняются как есть — они не участвуют в скоринге,
    но остаются в выходных данных.
    """
    normalized: Item = {}

    for raw_key, value in item.items():
        canonical = ALIAS_MAP.get(raw_key.lower().strip(), raw_key)
        normalized[canonical] = value

    return normalized


def extract_number(value: Any) -> Optional[float]:
    """
    Пытается извлечь число из значения любого типа.
    
    Примеры:
        3290        -> 3290.0
        "3 290 руб" -> 3290.0
        "4.5/5"     -> 4.5
        None        -> None
    """
    if value is None:
        return None

    if isinstance(value, (int, float)):
        return float(value)

    if isinstance(value, str):
        # Убираем пробелы-разделители тысяч и лишние символы
        cleaned = (
            value
            .replace("\u00a0", "")  # неразрывный пробел
            .replace(" ", "")
            .replace(",", ".")
        )

        # Берём первое число из строки (до слэша, пробела и т.д.)
        import re
        match = re.search(r"[\d]+\.?\d*", cleaned)

        if match:
            return float(match.group())

    return None


# ---------------------------------------------------------------------------
# Дедупликация
# ---------------------------------------------------------------------------

@dataclass
class DeduplicationConfig:
    """
    Настройки дедупликации.
    
    Два товара считаются одним, если совпадают все поля из match_fields.
    При схлопывании берём запись с лучшей ценой.
    """
    match_fields: List[str] = field(
        default_factory=lambda: ["бренд", "модель"]
    )
    prefer_cheapest: bool = True


def deduplicate(
    items: List[Item],
    config: DeduplicationConfig,
) -> List[Item]:
    """
    Схлопывает дубли из разных источников в одну запись.
    
    Логика:
    1. Группируем по ключу из match_fields.
    2. Внутри группы оставляем запись с лучшей ценой.
    3. Добавляем поле 'источники' — список всех источников, где товар найден.
       Чем больше источников подтвердили товар, тем выше доверие.
    """
    groups: Dict[str, List[Item]] = {}

    for item in items:
        key_parts = []

        for f in config.match_fields:
            val = item.get(f, "")
            key_parts.append(str(val).lower().strip())

        key = "||".join(key_parts)
        groups.setdefault(key, []).append(item)

    result: List[Item] = []

    for group_items in groups.values():
        if len(group_items) == 1:
            best = dict(group_items[0])
            best["_sources"] = [best.get("источник", "unknown")]
            best["_source_count"] = 1
            result.append(best)
            continue

        # Выбираем лучшую запись по цене
        def price_key(it: Item) -> float:
            p = extract_number(it.get("цена"))
            if p is None:
                # Нет цены — штрафуем, кидаем в конец при сортировке по цене
                return float("inf") if config.prefer_cheapest else float("-inf")
            return p

        sorted_group = sorted(
            group_items,
            key=price_key,
            reverse=not config.prefer_cheapest,
        )

        best = dict(sorted_group[0])
        best["_sources"] = [it.get("источник", "unknown") for it in group_items]
        best["_source_count"] = len(group_items)

        result.append(best)

    return result


# ---------------------------------------------------------------------------
# Скоринг
# ---------------------------------------------------------------------------

@dataclass
class ScoringConfig:
    """
    Настройки скоринга.
    
    weights:        веса полей (переопределяют глобальные SCORE_WEIGHTS)
    source_bonus:   бонус за каждый дополнительный источник, подтвердивший товар
    price_cap:      товары дороже этой суммы получают штраф (0 = выключено)
    """
    weights: Dict[str, float] = field(default_factory=lambda: dict(SCORE_WEIGHTS))
    source_bonus: float = 0.1
    price_cap: float = 0.0


def min_max_normalize(
    values: List[float],
    invert: bool = False,
) -> List[float]:
    """
    Нормализует список значений в диапазон [0, 1].
    
    invert=True: меньшее значение -> больший скор (используется для цены).
    
    Если все значения одинаковые — возвращаем 0.5 для всех.
    """
    if not values:
        return []

    min_v = min(values)
    max_v = max(values)

    if max_v == min_v:
        return [0.5] * len(values)

    normalized = [(v - min_v) / (max_v - min_v) for v in values]

    if invert:
        normalized = [1.0 - v for v in normalized]

    return normalized


def score_items(
    items: List[Item],
    config: ScoringConfig,
) -> List[Item]:
    """
    Вычисляет итоговый балл для каждого товара.
    
    Алгоритм:
    1. Для каждого числового поля из weights собираем все значения.
    2. Нормализуем значения по полю (min-max) с учётом направления.
    3. Умножаем на вес поля.
    4. Складываем взвешенные нормализованные оценки.
    5. Добавляем бонус за количество источников.
    6. Применяем штраф за цену выше price_cap.
    """
    if not items:
        return []

    # Шаг 1: собираем числовые значения по каждому полю
    field_values: Dict[str, List[Optional[float]]] = {}

    for field_name in config.weights:
        raw_vals = [extract_number(item.get(field_name)) for item in items]
        field_values[field_name] = raw_vals

    # Шаг 2: нормализуем каждое поле
    field_normalized: Dict[str, List[float]] = {}

    for field_name, raw_vals in field_values.items():
        # Заменяем None средним из имеющихся значений (мягкий impute)
        known = [v for v in raw_vals if v is not None]
        fallback = sum(known) / len(known) if known else 0.0

        filled = [v if v is not None else fallback for v in raw_vals]

        invert = not HIGHER_IS_BETTER.get(field_name, True)
        field_normalized[field_name] = min_max_normalize(filled, invert=invert)

    # Шаг 3-4: считаем взвешенный скор
    total_weight = sum(config.weights.values())

    for i, item in enumerate(items):
        weighted_sum = 0.0
        debug: Dict[str, Any] = {}

        for field_name, weight in config.weights.items():
            norm_score = field_normalized[field_name][i]
            contribution = norm_score * weight / total_weight

            weighted_sum += contribution
            debug[field_name] = {
                "raw": field_values[field_name][i],
                "normalized": round(norm_score, 4),
                "contribution": round(contribution, 4),
            }

        # Шаг 5: бонус за мультиисточниковость
        source_count = item.get("_source_count", 1)
        source_bonus = config.source_bonus * (source_count - 1)

        # Шаг 6: штраф за превышение price_cap
        price_penalty = 0.0

        if config.price_cap > 0:
            raw_price = extract_number(item.get("цена"))

            if raw_price is not None and raw_price > config.price_cap:
                # Линейный штраф: чем дороже cap, тем сильнее штраф
                overshoot = (raw_price - config.price_cap) / config.price_cap
                price_penalty = min(overshoot * 0.3, 0.5)  # не более 50%

        final_score = weighted_sum + source_bonus - price_penalty

        item["_score"] = round(final_score, 4)
        item["_debug"] = debug
        item["_debug"]["source_bonus"] = round(source_bonus, 4)
        item["_debug"]["price_penalty"] = round(price_penalty, 4)

    # Шаг 7: сортировка
    items.sort(key=lambda it: it["_score"], reverse=True)

    return items


# ---------------------------------------------------------------------------
# Публичный API
# ---------------------------------------------------------------------------

@dataclass
class RankerConfig:
    dedup: DeduplicationConfig = field(default_factory=DeduplicationConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)


def rank(
    raw_items: List[Item],
    config: Optional[RankerConfig] = None,
) -> List[Item]:
    """
    Главная функция. Принимает сырые данные с парсера, возвращает
    отсортированный список товаров с полем 'score'.
    
    Пайплайн:
        сырые данные
            -> нормализация полей
            -> дедупликация
            -> скоринг
            -> сортировка
    """
    if config is None:
        config = RankerConfig()

    # 1. Нормализация полей
    normalized = [normalize_item(item) for item in raw_items]

    # 2. Дедупликация
    deduplicated = deduplicate(normalized, config.dedup)

    # 3. Скоринг и сортировка
    scored = score_items(deduplicated, config.scoring)

    # 4. Чистый вывод
    result = []

    for item in scored:
        clean = {k: v for k, v in item.items() if not k.startswith("_")}
        clean["score"] = item["_score"]
        clean["источники"] = item.get("_sources", [])
        clean["отладка"] = item.get("_debug", {})
        result.append(clean)

    return result


# ---------------------------------------------------------------------------
# CLI / точка входа
# ---------------------------------------------------------------------------

def load_json(path: str) -> List[Item]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise ValueError("JSON должен содержать список")

    return data


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Товарный ранжировщик")
    parser.add_argument("--input",  default="data.json", help="Путь к JSON")
    parser.add_argument("--output", default="result.json", help="Куда сохранить результат")  # <- вот здесь
    parser.add_argument("--price-cap", type=float, default=0.0,
                        help="Штраф за товары дороже этой суммы (0=выкл)")
    parser.add_argument("--no-dedup", action="store_true",
                        help="Отключить дедупликацию")
    args = parser.parse_args()

    raw = load_json(args.input)

    config = RankerConfig(
        dedup=DeduplicationConfig(
            match_fields=["бренд", "модель"],
            prefer_cheapest=True,
        ) if not args.no_dedup else DeduplicationConfig(match_fields=[]),
        scoring=ScoringConfig(
            weights={
                "цена":     3.0,
                "рейтинг":  2.0,
                "отзывы":   1.5,
                "мощность": 1.0,
                "объём":    1.0,
            },
            source_bonus=0.1,
            price_cap=args.price_cap,
        ),
    )

    ranked = rank(raw, config)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(ranked, f, ensure_ascii=False, indent=2)
        print(f"Сохранено в {args.output}")
    else:
        print(json.dumps(ranked, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()