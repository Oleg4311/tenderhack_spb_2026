import json
import sys
import re
from difflib import SequenceMatcher
from statistics import mean, median
from typing import Any, Dict, List, Literal, Tuple, Optional, Union

SourceName = str
AggregationMode = Literal["mean", "sum", "median"]

DEFAULT_QUERY = "белая меховая зимняя куртка"

# Веса для специальных параметров
WEIGHT_PRICE = 0.2
WEIGHT_DISTANCE = 0.2
WEIGHT_TEXT_SIMILARITY = 0.2
WEIGHT_OTHER_PARAMS_TOTAL = 0.4


# ==================== УНИВЕРСАЛЬНАЯ РАБОТА С ЛЮБЫМИ ПОЛЯМИ ====================

def flatten_item(item: Dict[str, Any], source_name: str, prefix: str = "") -> Dict[str, Any]:
    """
    Рекурсивно разворачивает вложенный словарь в плоский.
    ВСЕ поля сохраняются, имена генерируются из пути.
    Пример: {"price": {"current": 100}} -> {"price.current": 100}
    """
    result = {"источник": source_name}
    
    def flatten(obj: Any, current_prefix: str = ""):
        if isinstance(obj, dict):
            for key, value in obj.items():
                # Пропускаем служебные поля, которые не нужны для сравнения
                if key in ["images", "links", "geo", "diagnostics", "error"]:
                    continue
                new_prefix = f"{current_prefix}.{key}" if current_prefix else key
                flatten(value, new_prefix)
        elif isinstance(obj, list):
            # Преобразуем список в строку для сравнения
            result[current_prefix] = json.dumps(obj, ensure_ascii=False, sort_keys=True)
        elif isinstance(obj, (int, float, str, bool, type(None))):
            result[current_prefix] = obj
        else:
            # Для любых других типов - строковое представление
            result[current_prefix] = str(obj)
    
    flatten(item)
    return result


def extract_all_items_from_search_results(data: Dict[str, Any]) -> Tuple[str, List[Dict[str, Any]], Dict[str, Any]]:
    """
    Извлекает ВСЕ товары из JSON в новом формате.
    Возвращает: (query, список_товаров, метаданные)
    """
    # Извлекаем запрос(ы)
    expanded_queries = data.get("expandedQueries", [])
    if expanded_queries:
        query = expanded_queries[0]
    else:
        query = data.get("normalizedQuery", data.get("query", DEFAULT_QUERY))
    
    # Извлекаем товары из всех источников
    sources = data.get("sources", {})
    all_items = []
    source_stats = {}
    
    for source_name, source_data in sources.items():
        status = source_data.get("status", "unknown")
        count = source_data.get("count", 0)
        items = source_data.get("items", [])
        error = source_data.get("error")
        
        source_stats[source_name] = {
            "status": status,
            "count": count,
            "error": error
        }
        
        if items:
            for item in items:
                flattened_item = flatten_item(item, source_name)
                all_items.append(flattened_item)
    
    meta = data.get("meta", {})
    summary = data.get("summary", {})
    
    return query, all_items, {
        "meta": meta,
        "summary": summary,
        "source_stats": source_stats,
        "all_queries": expanded_queries
    }


def dict_to_text_for_comparison(item: Dict[str, Any]) -> str:
    """
    Преобразует ВСЕ поля словаря в единую строку для сравнения.
    Исключает только служебные поля, которые не должны участвовать в сравнении.
    """
    parts = []
    
    # Поля, которые исключаем из текстового сравнения
    exclude_keys = {"источник", "_original_index", "_scores", "_debug", 
                    "_relevance_score", "_cv_aggregated_score", "_text_for_similarity"}
    
    for key, value in item.items():
        if key in exclude_keys or key.startswith("_"):
            continue
        if value is None:
            continue
        if isinstance(value, bool):
            parts.append(str(value).lower())
        elif isinstance(value, (int, float)):
            parts.append(str(value))
        else:
            parts.append(str(value).lower().strip())
    
    return " ".join(parts)


def extract_numeric_fields(items: List[Dict[str, Any]]) -> Tuple[List[float], List[float]]:
    """
    Извлекает ВСЕ числовые поля из товаров для последующей нормализации.
    Ищет поля, которые выглядят как цена или расстояние.
    """
    all_prices = []
    all_distances = []
    
    for item in items:
        for key, value in item.items():
            if value is None or key.startswith("_"):
                continue
            
            # Пытаемся извлечь число
            if isinstance(value, (int, float)):
                num_value = float(value)
                
                # Эвристика для определения цены (поля с "price", "current", "цена" в имени)
                if any(price_indicator in key.lower() for price_indicator in ["price", "current", "цена", "cost", "amount"]):
                    if 0 < num_value < 1000000:  # разумный диапазон цен
                        all_prices.append(num_value)
                
                # Эвристика для определения дальности (поля с "distance", "дальность", "delivery" в имени)
                elif any(dist_indicator in key.lower() for dist_indicator in ["distance", "дальность", "delivery", "dist"]):
                    if 0 < num_value < 10000:  # разумный диапазон расстояний
                        all_distances.append(num_value)
    
    return all_prices, all_distances


def extract_keywords(query: str) -> List[str]:
    """Извлекает ключевые слова из запроса"""
    words = re.findall(r'[а-яёa-z0-9]+', query.lower())
    return [w for w in words if len(w) > 2]


def calculate_price_score(item: Dict[str, Any], all_prices: List[float]) -> float:
    """
    Находит цену в любом поле товара и рассчитывает оценку.
    Чем МЕНЬШЕ цена → тем ВЫШЕ оценка.
    """
    # Ищем цену в любом поле
    price_value = None
    
    for key, value in item.items():
        if key.startswith("_"):
            continue
        if value is None:
            continue
        
        # Проверяем, похоже ли поле на цену
        if any(price_indicator in key.lower() for price_indicator in ["price", "current", "цена", "cost", "amount"]):
            if isinstance(value, (int, float)):
                price_value = float(value)
                break
            elif isinstance(value, str):
                # Пробуем извлечь число из строки
                match = re.search(r'[\d]+\.?\d*', value)
                if match:
                    price_value = float(match.group())
                    break
    
    if price_value is None:
        return 0.5
    
    if not all_prices:
        return 0.5
    
    min_price = min(all_prices)
    max_price = max(all_prices)
    
    if min_price == max_price:
        return 1.0 if price_value <= min_price else 0.0
    
    # Обратная нормализация: чем меньше цена, тем выше оценка
    score = 1.0 - (price_value - min_price) / (max_price - min_price)
    return max(0.0, min(1.0, score))


def calculate_distance_score(item: Dict[str, Any], all_distances: List[float]) -> float:
    """
    Находит расстояние в любом поле товара и рассчитывает оценку.
    Чем МЕНЬШЕ расстояние → тем ВЫШЕ оценка.
    """
    # Ищем расстояние в любом поле
    distance_value = None
    
    for key, value in item.items():
        if key.startswith("_"):
            continue
        if value is None:
            continue
        
        # Проверяем, похоже ли поле на расстояние
        if any(dist_indicator in key.lower() for dist_indicator in ["distance", "дальность", "delivery", "dist", "км"]):
            if isinstance(value, (int, float)):
                distance_value = float(value)
                break
            elif isinstance(value, str):
                # Пробуем извлечь число из строки
                match = re.search(r'[\d]+\.?\d*', value)
                if match:
                    distance_value = float(match.group())
                    break
    
    if distance_value is None:
        return 0.5
    
    if not all_distances:
        return 0.5
    
    min_dist = min(all_distances)
    max_dist = max(all_distances)
    
    if min_dist == max_dist:
        return 1.0 if distance_value <= min_dist else 0.0
    
    # Обратная нормализация: чем меньше расстояние, тем выше оценка
    score = 1.0 - (distance_value - min_dist) / (max_dist - min_dist)
    return max(0.0, min(1.0, score))


def calculate_text_similarity(item_text: str, query: str) -> float:
    """Рассчитывает похожесть текста товара на запрос"""
    if not item_text or not query:
        return 0.0
    return SequenceMatcher(None, item_text.lower(), query.lower()).ratio()


def calculate_keyword_match_score(item: Dict[str, Any], keywords: List[str], weights: Dict[str, float]) -> float:
    """Рассчитывает взвешенную оценку совпадения с ключевыми словами во ВСЕХ полях"""
    if not keywords or not weights:
        return 0.0
    
    # Собираем все значения товара в одну строку
    all_values = []
    for key, value in item.items():
        if key.startswith("_") or key == "источник":
            continue
        if value is None:
            continue
        all_values.append(str(value).lower())
    
    combined_text = " ".join(all_values)
    
    total_score = 0.0
    total_weight = sum(weights.values())
    
    if total_weight == 0:
        return 0.0
    
    for keyword, weight in weights.items():
        if keyword in combined_text:
            total_score += weight
    
    return total_score / total_weight


def group_by_source(items: List[Dict[str, Any]]) -> Dict[SourceName, List[Dict[str, Any]]]:
    """Группирует объекты по источнику"""
    grouped: Dict[SourceName, List[Dict[str, Any]]] = {}
    
    for original_index, item in enumerate(items):
        source = item.get("источник", "unknown")
        
        enriched_item = dict(item)
        enriched_item["_original_index"] = original_index
        enriched_item["_text_for_similarity"] = dict_to_text_for_comparison(item)
        enriched_item["_scores"] = []
        enriched_item["_debug"] = {}
        enriched_item["_relevance_score"] = 0.0
        
        grouped.setdefault(source, []).append(enriched_item)
    
    return grouped


def get_keyword_weights(keywords: List[str]) -> Dict[str, float]:
    """Распределяет вес 0.4 между ключевыми словами поровну"""
    if not keywords:
        return {}
    weight_per_keyword = WEIGHT_OTHER_PARAMS_TOTAL / len(keywords)
    return {kw: weight_per_keyword for kw in keywords}


def calculate_comprehensive_score(
    item: Dict[str, Any],
    keywords: List[str],
    keyword_weights: Dict[str, float],
    all_prices: List[float],
    all_distances: List[float],
    query: str
) -> float:
    """Вычисляет комплексную оценку на основе ВСЕХ полей товара"""
    
    # 1. Оценка по цене (ищем в любом поле)
    price_score = calculate_price_score(item, all_prices)
    
    # 2. Оценка по дальности (ищем в любом поле)
    distance_score = calculate_distance_score(item, all_distances)
    
    # 3. Оценка текстовой похожести (все поля)
    item_text = dict_to_text_for_comparison(item)
    text_similarity = calculate_text_similarity(item_text, query)
    
    # 4. Оценка совпадения ключевых слов (все поля)
    keyword_score = calculate_keyword_match_score(item, keywords, keyword_weights)
    
    # 5. Если есть готовая оценка relevance в данных - используем её
    ready_relevance = None
    for key, value in item.items():
        if "relevance" in key.lower() and isinstance(value, (int, float)):
            ready_relevance = float(value)
            break
    
    if ready_relevance is not None:
        text_similarity = (text_similarity + ready_relevance) / 2
    
    # Итоговая оценка
    total_score = (
        WEIGHT_PRICE * price_score +
        WEIGHT_DISTANCE * distance_score +
        WEIGHT_TEXT_SIMILARITY * text_similarity +
        keyword_score
    )
    
    # Отладочная информация
    item["_debug"] = {
        "price_score": round(price_score, 4),
        "distance_score": round(distance_score, 4),
        "text_similarity": round(text_similarity, 4),
        "keyword_score": round(keyword_score, 4),
        "total_score": round(total_score, 4),
        "ready_relevance": ready_relevance
    }
    
    return total_score


def run_relevance_ranking(grouped: Dict[SourceName, List[Dict[str, Any]]], query: str) -> None:
    """Ранжирует объекты по релевантности"""
    keywords = extract_keywords(query)
    print(f"\n🔍 Ключевые слова запроса: {keywords}")
    
    keyword_weights = get_keyword_weights(keywords)
    print(f"📊 Веса ключевых слов: {keyword_weights}")
    
    # Собираем все цены и расстояния из ВСЕХ товаров
    all_items = []
    for items in grouped.values():
        all_items.extend(items)
    
    all_prices, all_distances = extract_numeric_fields(all_items)
    
    print(f"\n💰 Найдено цен: {len(all_prices)}")
    if all_prices:
        print(f"   Диапазон: от {min(all_prices):,} до {max(all_prices):,} руб.")
        print(f"   (чем меньше цена → тем выше оценка)")
    
    print(f"📍 Найдено расстояний: {len(all_distances)}")
    if all_distances:
        print(f"   Диапазон: от {min(all_distances)} до {max(all_distances)} км")
        print(f"   (чем меньше дальность → тем выше оценка)")
    
    # Вычисляем оценки для каждого товара
    for source, items in grouped.items():
        for item in items:
            score = calculate_comprehensive_score(
                item, keywords, keyword_weights,
                all_prices, all_distances, query
            )
            item["_relevance_score"] = score


def priority_score(index_in_source: int, total_in_source: int) -> float:
    if total_in_source <= 1:
        return 1.0
    return 1.0 - (index_in_source / (total_in_source - 1))


def string_similarity_simple(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def run_cross_validation_iteration(reference_source: SourceName, grouped: Dict[SourceName, List[Dict[str, Any]]]) -> None:
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
            continue
        
        for item in items:
            similarities = [
                string_similarity_simple(item["_text_for_similarity"], ref_item["_text_for_similarity"]) 
                for ref_item in reference_items
            ]
            best_score = max(similarities) if similarities else 0.0
            item["_scores"].append(best_score)


def run_full_cross_validation(grouped: Dict[SourceName, List[Dict[str, Any]]]) -> None:
    sources = list(grouped.keys())
    for source in sources:
        if source in grouped:
            run_cross_validation_iteration(source, grouped)


def aggregate_scores(grouped: Dict[SourceName, List[Dict[str, Any]]], mode: AggregationMode = "mean") -> None:
    for source, items in grouped.items():
        for item in items:
            scores = item["_scores"]
            if not scores:
                agg_score = 0.0
            elif mode == "mean":
                agg_score = mean(scores)
            elif mode == "median":
                agg_score = median(scores)
            else:
                agg_score = sum(scores)
            item["_cv_aggregated_score"] = agg_score


def find_duplicates_by_threshold(grouped: Dict[SourceName, List[Dict[str, Any]]], threshold: float = 0.7) -> List[Dict[str, Any]]:
    """Находит дубликаты между разными источниками на основе ВСЕХ полей"""
    duplicates = []
    sources = list(grouped.keys())
    
    for i, source1 in enumerate(sources):
        for source2 in sources[i+1:]:
            for item1 in grouped[source1]:
                for item2 in grouped[source2]:
                    similarity = string_similarity_simple(
                        item1["_text_for_similarity"], 
                        item2["_text_for_similarity"]
                    )
                    if similarity >= threshold:
                        # Пытаемся найти идентификаторы товаров
                        id1 = None
                        id2 = None
                        for key in ["id", "sku", "article", "Артикул", "productId"]:
                            if key in item1 and id1 is None:
                                id1 = item1.get(key)
                            if key in item2 and id2 is None:
                                id2 = item2.get(key)
                        
                        duplicates.append({
                            "source1": source1,
                            "source2": source2,
                            "similarity": similarity,
                            "item1_id": id1 or str(item1.get("_original_index", "?")),
                            "item2_id": id2 or str(item2.get("_original_index", "?")),
                            "item1_title": item1.get("title", item1.get("name", "N/A"))[:50],
                            "item2_title": item2.get("title", item2.get("name", "N/A"))[:50]
                        })
    
    return duplicates


def sort_by_relevance(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(items, key=lambda x: x.get("_relevance_score", 0), reverse=True)


def print_source_stats(source_stats: Dict[str, Any]):
    """Выводит статистику по источникам"""
    print("\n" + "=" * 70)
    print("СТАТИСТИКА ПО ИСТОЧНИКАМ")
    print("=" * 70)
    
    for source_name, stats in source_stats.items():
        status = stats.get("status", "unknown")
        count = stats.get("count", 0)
        error = stats.get("error")
        
        status_icon = {
            "ok": "✅",
            "empty": "📭",
            "blocked": "🚫",
            "error": "❌"
        }.get(status, "❓")
        
        print(f"\n{status_icon} {source_name.upper()}")
        print(f"   Статус: {status}")
        print(f"   Товаров: {count}")
        if error:
            print(f"   Ошибка: {error.get('type', 'unknown')}")


def print_summary(summary: Dict[str, Any]):
    """Выводит сводку"""
    print("\n" + "=" * 70)
    print("СВОДКА")
    print("=" * 70)
    
    total_found = summary.get("totalFound", 0)
    successful = summary.get("successfulSources", 0)
    failed = summary.get("failedSources", 0)
    empty = summary.get("emptySources", 0)
    
    print(f"\n📊 Всего найдено товаров: {total_found}")
    print(f"   Успешных источников: {successful}")
    print(f"   Пустых источников: {empty}")
    print(f"   Неудачных источников: {failed}")
    
    price_info = summary.get("price", {})
    if price_info:
        print(f"\n💰 Цены: от {price_info.get('min', 'N/A')} до {price_info.get('max', 'N/A')} {price_info.get('currency', 'RUB')}")


def print_statistics(grouped: Dict[SourceName, List[Dict[str, Any]]]):
    """Выводит статистику по обработанным товарам"""
    print("\n" + "=" * 70)
    print("СТАТИСТИКА ПО ОБРАБОТАННЫМ ТОВАРАМ")
    print("=" * 70)
    
    for source, items in grouped.items():
        print(f"\n📦 Источник: {source.upper()}")
        print(f"   Количество: {len(items)}")
        
        if items:
            scores = [item.get("_relevance_score", 0) for item in items]
            cv_scores = [item.get("_cv_aggregated_score", 0) for item in items]
            
            print(f"   🎯 Средняя релевантность: {mean(scores):.3f}")
            print(f"   📈 Макс релевантность: {max(scores):.3f}")
            print(f"   📉 Мин релевантность: {min(scores):.3f}")
            print(f"   🔄 Средняя кросс-валидация: {mean(cv_scores):.3f}")


def main(file_path: str = "search_results.json"):
    """
    Главная функция. Загружает JSON, извлекает expandedQueries
    и сравнивает ВСЕ поля товаров.
    """
    print("=" * 70)
    print("УНИВЕРСАЛЬНЫЙ ВАЛИДАТОР JSON")
    print("Сравниваются ВСЕ поля товаров (без привязки к именам ключей)")
    print("=" * 70)
    
    try:
        with open(file_path, "r", encoding="utf-8") as file:
            raw_data = json.load(file)
        
        if not isinstance(raw_data, dict):
            raise ValueError("JSON должен содержать словарь")
        
        # Извлекаем ВСЕ товары (универсально)
        query, items, metadata = extract_all_items_from_search_results(raw_data)
        
        print(f"\n🔎 Запрос: \"{query}\"")
        
        all_queries = metadata.get("all_queries", [query])
        if len(all_queries) > 1:
            print(f"📝 Расширенные запросы: {all_queries}")
        
        print(f"\n✅ Извлечено товаров: {len(items)}")
        
        # Статистика по источникам
        print_source_stats(metadata.get("source_stats", {}))
        print_summary(metadata.get("summary", {}))
        
        if not items:
            print("\n⚠️ Нет товаров для анализа!")
            return
        
    except FileNotFoundError:
        print(f"\n❌ Файл {file_path} не найден!")
        return
    except Exception as e:
        print(f"\n❌ Ошибка: {e}")
        return
    
    print("\n⭐ Правила оценки (на основе ВСЕХ полей товара):")
    print("   1. Цена: ищется в любом поле (price, current, цена и т.д.) → чем меньше, тем лучше")
    print("   2. Дальность: ищется в любом поле (distance, дальность, delivery и т.д.) → чем меньше, тем лучше")
    print("   3. Текст: сравниваются ВСЕ текстовые поля товара с запросом")
    print("   4. Ключевые слова: ищутся во ВСЕХ полях товара")
    
    # Группировка по источникам
    grouped = group_by_source(items)
    print("\n📊 Распределение по источникам:")
    for source, source_items in grouped.items():
        print(f"   {source}: {len(source_items)} товаров")
    
    # Ранжирование по релевантности
    run_relevance_ranking(grouped, query)
    
    # Кросс-валидация
    run_full_cross_validation(grouped)
    aggregate_scores(grouped, mode="mean")
    
    # Поиск дубликатов
    duplicates = find_duplicates_by_threshold(grouped, threshold=0.7)
    
    # Статистика
    print_statistics(grouped)
    
    if duplicates:
        print(f"\n🔍 Найдено потенциальных дубликатов: {len(duplicates)}")
        for i, dup in enumerate(duplicates[:5]):
            print(f"   {i+1}. {dup['source1']} ↔ {dup['source2']}: {dup['similarity']:.1%}")
            print(f"      {dup['item1_title'][:40]}...")
    
    # Сортировка и сохранение
    all_items = []
    for source, source_items in grouped.items():
        all_items.extend(source_items)
    
    sorted_items = sort_by_relevance(all_items)
    
    # Очистка и сохранение результатов
    cleaned_items = []
    for item in sorted_items:
        cleaned = {k: v for k, v in item.items() if not k.startswith("_")}
        cleaned["_рейтинг_релевантности"] = round(item.get("_relevance_score", 0), 4)
        cleaned["_кросс_оценка"] = round(item.get("_cv_aggregated_score", 0), 4)
        cleaned_items.append(cleaned)
    
    output_file = "products_sorted_by_relevance.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(cleaned_items, f, ensure_ascii=False, indent=2)
    
    # Топ-10
    print("\n" + "=" * 70)
    print("🏆 ТОП-10 САМЫХ РЕЛЕВАНТНЫХ ТОВАРОВ")
    print("=" * 70)
    
    for i, item in enumerate(cleaned_items[:10]):
        # Пытаемся найти название товара в любом поле
        title = item.get("title", item.get("name", item.get("Тип", "N/A")))
        brand = item.get("brand", item.get("Бренд в одежде и обуви", "N/A"))
        
        # Пытаемся найти цену
        price = "N/A"
        for key, value in item.items():
            if any(p in key.lower() for p in ["price", "current", "цена"]):
                if isinstance(value, (int, float)):
                    price = f"{value:,.0f}"
                    break
                elif isinstance(value, str):
                    price = value
                    break
        
        print(f"\n{i+1}. {item.get('источник', 'N/A').upper()}")
        print(f"   📝 {str(title)[:60]}")
        print(f"   🏷️ Бренд: {brand}")
        print(f"   💰 Цена: {price} руб.")
        print(f"   🎯 Оценка: {item.get('_рейтинг_релевантности', 0):.4f}")
    
    print(f"\n✨ Готово! Результаты сохранены в {output_file}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        main(sys.argv[1])
    else:
        main("search_results.json")