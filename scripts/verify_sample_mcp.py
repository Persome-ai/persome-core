"""Verify the synthetic sample through the real streamable HTTP MCP transport."""

from __future__ import annotations

import argparse
import asyncio
import json

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

REQUIRED_TOOLS = {"correct_memory", "get_model_snapshot", "read_receipt", "search"}
QUERY = "When does the user prefer focused writing?"


async def verify(url: str) -> dict:
    async with (
        streamable_http_client(url) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        listed = await session.list_tools()
        tool_names = {tool.name for tool in listed.tools}
        missing = sorted(REQUIRED_TOOLS - tool_names)
        if missing:
            raise RuntimeError(f"required MCP tools missing: {', '.join(missing)}")

        search_result = await session.call_tool(
            "search",
            {"query": QUERY, "top_k": 2},
        )
        search_payload = json.loads(search_result.content[0].text)
        top = search_payload["results"][0]

        receipt_result = await session.call_tool(
            "read_receipt",
            {"entry_id": top["id"]},
        )
        receipt = json.loads(receipt_result.content[0].text)
        for key in ("id", "path", "content"):
            if receipt[key] != top[key]:
                raise RuntimeError(f"receipt mismatch for {key}")

        return {
            "endpoint": url,
            "tool_count": len(tool_names),
            "required_tools": sorted(REQUIRED_TOOLS),
            "top_result": {
                "id": top["id"],
                "path": top["path"],
                "timestamp": top["timestamp"],
                "content": top["content"],
            },
            "receipt_verified": True,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8743/mcp")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(verify(args.url)), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
