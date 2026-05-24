import asyncio
from typing import Literal

from pydantic import BaseModel


Marketplace = Literal["ozon", "wildberries", "yandex_market", "other"]


class AggregatorProduct(BaseModel):
    title: str
    price: float | None
    old_price: float | None = None
    marketplace: Marketplace
    product_url: str | None = None
    image_url: str | None = None
    rating: float | None = None
    reviews_count: int | None = None
    source: str


class AggregatorSearchResult(BaseModel):
    source: str
    query: str
    page: int
    products: list[AggregatorProduct]


class BaseAggregatorParser:
    source = "base"
    usable_for_project = False
    requires_auth = True
    notes = "Not researched"

    async def search(self, query: str, page: int = 1) -> AggregatorSearchResult:
        raise NotImplementedError

    async def empty(self, query: str, page: int = 1) -> AggregatorSearchResult:
        await asyncio.sleep(0)
        return AggregatorSearchResult(source=self.source, query=query, page=page, products=[])
