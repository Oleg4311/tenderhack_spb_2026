import json
from difflib import SequenceMatcher
from statistics import mean, median
from typing import Any, Dict, List, Literal


SourceName = str
AggregationMode = Literal["mean", "sum", "median"]


DEFAULT_SOURCES = ["Wildberries", "Ozon", "Yandex Market", "other"]


def load_json(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data, list):
        raise ValueError("JSON должен содержать список словарей")

    for item in data:
        if not isinstance(item, dict):
            raise ValueError("Каждый элемент JSON должен быть словарём")
        if "источник" not in item:
            raise ValueError(f"У словаря нет ключа 'источник': {item}")

    return data


def dict_to_text(item: Dict[str, Any]) -> str:
    parts = []
    
    exclude_keys = {"источник", "Артикул", "отладка", "_original_index", "_text", "_scores", "_debug"}

    for key, value in item.items():
        if key in exclude_keys:
            continue
        if value is None:
            continue
        parts.append(str(value))

    return " ".join(parts).lower().strip()


def string_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def group_by_source(items: List[Dict[str, Any]]) -> Dict[SourceName, List[Dict[str, Any]]]:
    grouped: Dict[SourceName, List[Dict[str, Any]]] = {}

    for original_index, item in enumerate(items):
        source = item["источник"]

        enriched_item = dict(item)
        enriched_item["_original_index"] = original_index
        enriched_item["_text"] = dict_to_text(item)
        enriched_item["_scores"] = []
        enriched_item["_debug"] = {}

        grouped.setdefault(source, []).append(enriched_item)

    return grouped


def priority_score(index_in_source: int, total_in_source: int) -> float:
    if total_in_source <= 1:
        return 1.0
    return 1.0 - (index_in_source / (total_in_source - 1))


def run_cross_validation_iteration(
    reference_source: SourceName,
    grouped: Dict[SourceName, List[Dict[str, Any]]],
) -> None:
    reference_items = grouped.get(reference_source, [])

    if not reference_items:
        return

    for source, items in grouped.items():
        if not items:
            continue

        if source == reference_source:
            total = len(items)
            for index, item in enumerate(items):
                score = priority_score(index, total)
                item["_scores"].append(score)
                item["_debug"][f"iteration_{reference_source}"] = {
                    "role": "reference",
                    "score": score,
                }
            continue

        for item in items:
            similarities = [
                string_similarity(item["_text"], reference_item["_text"])
                for reference_item in reference_items
            ]
            best_score = max(similarities) if similarities else 0.0
            item["_scores"].append(best_score)
            item["_debug"][f"iteration_{reference_source}"] = {
                "role": "compared",
                "score": best_score,
            }


def add_description_score(
    grouped: Dict[SourceName, List[Dict[str, Any]]],
    description: str,
    weight: float = 1.0,
) -> None:
    description = description.lower().strip()

    for items in grouped.values():
        for item in items:
            score = string_similarity(item["_text"], description) * weight
            item["_scores"].append(score)
            item["_debug"]["description"] = {
                "role": "description_match",
                "score": score,
            }


def aggregate_scores(
    scores: List[float],
    mode: AggregationMode = "mean",
) -> float:
    if not scores:
        return 0.0

    if mode == "mean":
        return mean(scores)
    if mode == "sum":
        return sum(scores)
    if mode == "median":
        return median(scores)

    raise ValueError(f"Неизвестный режим агрегации: {mode}")


def rank_items(
    items: List[Dict[str, Any]],
    description: str,
    sources: List[SourceName] = None,
    aggregation: AggregationMode = "mean",
    description_weight: float = 1.0,
) -> List[Dict[str, Any]]:
    if sources is None:
        sources = DEFAULT_SOURCES

    grouped = group_by_source(items)

    for source in sources:
        run_cross_validation_iteration(source, grouped)

    add_description_score(
        grouped=grouped,
        description=description,
        weight=description_weight,
    )

    result = []

    for source_items in grouped.values():
        for item in source_items:
            final_score = aggregate_scores(item["_scores"], aggregation)

            clean_item = {
                key: value
                for key, value in item.items()
                if not key.startswith("_")
            }

            clean_item["итоговый_балл"] = round(final_score, 4)
            clean_item["отладка"] = item["_debug"]

            result.append(clean_item)

    result.sort(
        key=lambda item: item["итоговый_балл"],
        reverse=True,
    )

    return result


def print_ranked_items(items: List[Dict[str, Any]]) -> None:
    for index, item in enumerate(items, start=1):
        print(f"\n{'='*60}")
        print(f"{index}. Балл: {item['итоговый_балл']}")
        print(f"{'='*60}")
        
        for key, value in item.items():
            if key not in {"отладка", "итоговый_балл"}:
                value_str = str(value)
                if len(value_str) > 80:
                    value_str = value_str[:80] + "..."
                print(f"   {key}: {value_str}")


def main() -> None:
    # ИСПРАВЛЕНО: используем ваш файл json.json
    json_path = "json.json"
    
    # Пример описания (можете поменять)
    description = "черный смарт-часы с gps пульсометр"
    
    try:
        items = load_json(json_path)
    except FileNotFoundError:
        print(f"Ошибка: файл '{json_path}' не найден!")
        return
    
    print(f"Загружено товаров: {len(items)}")
    
    sources_found = set(item.get("источник") for item in items)
    print(f"Найдены источники: {sources_found}")

    ranked_items = rank_items(
        items=items,
        description=description,
        sources=DEFAULT_SOURCES,
        aggregation="mean",
        description_weight=1.5,
    )

    print_ranked_items(ranked_items)
    
    output_path = "ranked_result.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(ranked_items, f, ensure_ascii=False, indent=2)
    
    print(f"\nРезультат сохранён в файл: {output_path}")


if __name__ == "__main__":
    main()