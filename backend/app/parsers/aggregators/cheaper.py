from app.parsers.aggregators.base import AggregatorSearchResult, BaseAggregatorParser


class CheaperParser(BaseAggregatorParser):
    source = "cheaper"
    usable_for_project = False
    requires_auth = False
    notes = "cheaper.ru timed out during web/XHR research; no public unauthenticated endpoint found"

    async def search(self, query: str, page: int = 1) -> AggregatorSearchResult:
        return await self.empty(query, page)
