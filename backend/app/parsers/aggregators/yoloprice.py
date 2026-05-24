from app.parsers.aggregators.base import AggregatorSearchResult, BaseAggregatorParser


class YoloPriceParser(BaseAggregatorParser):
    source = "yoloprice"
    usable_for_project = False
    requires_auth = True
    notes = "web is landing-only; APK exposes mobile gRPC/SDK paths, not public web JSON"

    async def search(self, query: str, page: int = 1) -> AggregatorSearchResult:
        return await self.empty(query, page)
