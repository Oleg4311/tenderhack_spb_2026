"""
SearxNG fallback dispatcher.

Вызывается из service.py когда основной парсер вернул status="blocked".
Ищет товары через SearxNG (Google/Yandex site:маркетплейс) и парсит карточки.
"""

import asyncio
import logging

from app.parsers.common import SourceResult

logger = logging.getLogger(__name__)


async def searxng_fallback(source: str, query: str, region: str, limit: int, category: str) -> SourceResult | None:
    """
    Единая точка входа для SearxNG fallback по любому источнику.
    Вызывается когда основной парсер вернул status="blocked".
    """
    try:
        if source == "wildberries":
            from app.parsers.wildberries_fix import wb_searxng_fallback
            items = await asyncio.wait_for(
                wb_searxng_fallback(query, region, limit, category),
                timeout=20,
            )
        elif source == "ozon":
            from app.parsers.ozon_fix import ozon_searxng_fallback
            items = await asyncio.wait_for(
                ozon_searxng_fallback(query, region, limit, category),
                timeout=25,
            )
        elif source == "yandex_market":
            from app.parsers.ym_fix import ym_searxng_fallback
            items = await asyncio.wait_for(
                ym_searxng_fallback(query, region, limit, category),
                timeout=25,
            )
        else:
            return None

        if items:
            logger.info(f"[SearxNG fallback] {source}: {len(items)} items recovered")
            return SourceResult(source, "ok", len(items), "", items)
        return None

    except asyncio.TimeoutError:
        logger.warning(f"[SearxNG fallback] {source}: timeout")
        return None
    except ImportError as e:
        logger.warning(f"[SearxNG fallback] {source}: import error: {e}")
        return None
    except Exception as e:
        logger.warning(f"[SearxNG fallback] {source}: {e}")
        return None
