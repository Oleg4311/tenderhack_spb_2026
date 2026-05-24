import asyncio

from app.parsers.aggregators.base import AggregatorProduct, AggregatorSearchResult, BaseAggregatorParser
from app.parsers.aggregators.cheaper import CheaperParser
from app.parsers.aggregators.palert import PalertParser
from app.parsers.aggregators.yoloprice import YoloPriceParser


PARSERS: tuple[type[BaseAggregatorParser], ...] = (
    CheaperParser,
    YoloPriceParser,
    PalertParser,
)


def usable_parsers() -> list[BaseAggregatorParser]:
    return [parser() for parser in PARSERS if getattr(parser, "usable_for_project", False)]


async def search_all_aggregators(query: str, page: int = 1) -> AggregatorSearchResult:
    parsers = usable_parsers()
    if not parsers:
        return AggregatorSearchResult(source="aggregators", query=query, page=page, products=[])

    results = await asyncio.gather(
        *(parser.search(query, page=page) for parser in parsers),
        return_exceptions=True,
    )
    products: list[AggregatorProduct] = []
    for result in results:
        if isinstance(result, AggregatorSearchResult):
            products.extend(result.products)
    return AggregatorSearchResult(
        source="aggregators",
        query=query,
        page=page,
        products=_dedupe(products),
    )


def _dedupe(products: list[AggregatorProduct]) -> list[AggregatorProduct]:
    seen = set()
    out: list[AggregatorProduct] = []
    for product in products:
        key = (
            product.title.strip().lower(),
            product.marketplace,
            product.price,
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(product)
    return out
