from app.parsers.aggregators.base import AggregatorSearchResult, BaseAggregatorParser


class PalertParser(BaseAggregatorParser):
    source = "palert"
    usable_for_project = False
    requires_auth = True
    notes = "API examples require Bearer auth; name search is Pro/auth gated"

    async def search(self, query: str, page: int = 1) -> AggregatorSearchResult:
        return await self.empty(query, page)
