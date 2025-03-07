import asyncio
import json
import os
from typing import Any, Literal

import weave
from pydantic import BaseModel, Field, PrivateAttr
from tavily import AsyncTavilyClient

from utils import format_doc


class SearchInternet(BaseModel):
    """Tool to search the internet for information"""

    search_query: str = Field(description="The query to search the internet for")

    @weave.op
    async def run(self) -> dict[str, str]:
        tavily_client = AsyncTavilyClient(api_key=os.environ["TAVILY_API_KEY"])
        context = await tavily_client.get_search_context(query=self.search_query)
        results = [json.loads(item) for item in json.loads(json.loads(context))]
        context_str = ""
        for result in results:
            for k, v in result.items():
                context_str += f"{k}: {v}" + "\n\n"
        return {"results": context_str}


class SearchDocumentation(BaseModel):
    """Searches financial documents and SEC filings. Use this tool for queries related to company financial reports, earnings calls, and regulatory filings."""

    search_query: str = Field(
        ...,
        description="A detailed query to search financial documents and SEC filings",
    )
    num_results: int = Field(10, description="The number of results to return")
    docs_db: str = "finance-docs"
    pinecone_env: str = Field("gcp-starter", description="Pinecone environment")
    _retriever: Any = PrivateAttr()

    def model_post_init(self, _context: Any) -> None:
        from retriever import RetrieverWithReranker, VectorStoreSearchEngine

        search_engine = VectorStoreSearchEngine(
            index_name=self.docs_db,
            environment=self.pinecone_env
        )
        search_engine = asyncio.run(search_engine.load())

        self._retriever = RetrieverWithReranker(search_engine=search_engine)

    @weave.op
    async def run(self) -> dict[str, str]:
        results = await self._retriever.invoke(
            self.search_query, top_k=self.num_results * 2, top_n=self.num_results
        )
        return {"results": "\n\n".join([format_doc(result) for result in results])}

tools = [
    SearchInternet(search_query=""),
    SearchDocumentation(search_query="", num_results=5),
]


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": tool.model_json_schema()["title"],
            "description": tool.model_json_schema()["description"],
            "parameters": {
                "type": "object",
                "properties": tool.model_json_schema()["properties"],
                "required": tool.model_json_schema()["required"],
            },
        },
    }
    for tool in tools
]
FUNCTION_MAP = {
    "SearchInternet": SearchInternet,
    "SearchDocumentation": SearchDocumentation,
}
