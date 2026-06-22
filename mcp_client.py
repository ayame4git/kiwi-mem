"""
mcp_client.py — MCP 客戶端模組（v2）
==================================
讓 gateway 作為 MCP 用戶端，連接外部 MCP 伺服器，取得工具並執行呼叫。

支援傳輸格式：
  -Streamable HTTP（遠端部署，主要場景）
  - SSE（舊版遠端相容）
  - stdio（本地進程，預留介面）

核心功能：
  - 連接 MCP 伺服器，列出可用工具
  - 將 MCP 工具 schema 轉換為 OpenAI function calling 格式
  - 執行 tool_call，回傳結果
  - 工具清單快取（避免每次請求都重新連線）
"""

import json
import time
import asyncio
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.client.sse import sse_client

# ============================================================
# 工具缓存
# ============================================================

# { url: { "tools": [...], "mcp_tools": [...], "timestamp": float } }
_tool_cache: dict = {}
_CACHE_TTL = 300  # 5 分钟缓存


def _cache_valid(url: str) -> bool:
    if url not in _tool_cache:
        return False
    return (time.time() - _tool_cache[url]["timestamp"]) < _CACHE_TTL


def clear_tool_cache(url: str = None):
    """清除工具快取"""
    if url:
        _tool_cache.pop(url, None)
    else:
        _tool_cache.clear()


# ============================================================
# 連接 MCP 伺服器 & 列出工具
# ============================================================

async def _connect_and_list(url: str, transport: str = "streamable_http") -> list:
    """
    連接 MCP 伺服器並取得工具列表
    傳回 MCP Tool 對象列表
    """
    try:
        if transport == "sse":
            async with sse_client(url) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.list_tools()
                    return list(result.tools)
        else:
            # streamable_http (default)
            async with streamablehttp_client(url) as (read_stream, write_stream, _):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.list_tools()
                    return list(result.tools)
    except Exception as e:
        print(f"❌ MCP 連線失敗 [{url}]: {e}")
        return []


def _mcp_tool_to_openai(tool, server_url: str) -> dict:
    """
    將 MCP Tool schema 轉換為 OpenAI function calling 格式
    在 function name 中不包含 server 資訊（MCP tool name 本身已有前綴）
    """
    # 处理 inputSchema
    input_schema = tool.inputSchema or {}
    if isinstance(input_schema, dict):
        # 确保有 type: object
        schema = {**input_schema}
        if "type" not in schema:
            schema["type"] = "object"
    else:
        schema = {"type": "object", "properties": {}}

    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": (tool.description or "")[:1024],
            "parameters": schema,
        },
    }


async def get_tools_for_servers(servers: list[dict]) -> tuple[list[dict], dict]:
    """
    取得多個 MCP 伺服器的工具列表

    參數：
      servers: [{"url": "https://...", "transport": "streamable_http", "name": "..."}, ...]

    回傳：
      (openai_tools, tool_map)
      - openai_tools: OpenAI function calling 格式的工具列表
      - tool_map: { tool_name: {"url": server_url, "transport": transport} }
    """
    openai_tools = []
    tool_map = {}  # tool_name → server info

    for server in servers:
        url = server.get("url", "")
        transport = server.get("transport", "streamable_http")
        name = server.get("name", url)

        if not url:
            continue

        # 檢查快取
        if _cache_valid(url):
            cached = _tool_cache[url]
            for oa_tool in cached["tools"]:
                tool_name = oa_tool["function"]["name"]
                openai_tools.append(oa_tool)
                tool_map[tool_name] = {"url": url, "transport": transport, "server_name": name}
            print(f"🔧 MCP [{name}]: {len(cached['tools'])} 工具 (快取)")
            continue

        # 連接獲取
        print(f"🔧 MCP [{name}]: 連線中...")
        mcp_tools = await _connect_and_list(url, transport)

        if mcp_tools:
            oa_tools = []
            for t in mcp_tools:
                oa_tool = _mcp_tool_to_openai(t, url)
                oa_tools.append(oa_tool)
                tool_map[t.name] = {"url": url, "transport": transport, "server_name": name}
            
            openai_tools.extend(oa_tools)

            # 快取
            _tool_cache[url] = {
                "tools": oa_tools,
                "mcp_tools": mcp_tools,
                "timestamp": time.time(),
            }
            print(f"🔧 MCP [{name}]: {len(oa_tools)} 工具 ✓")
        else:
            print(f"⚠️ MCP [{name}]: 無工具或連線失敗")

    return openai_tools, tool_map


# ============================================================
# 执行工具调用
# ============================================================

async def call_tool(tool_name: str, arguments: dict, tool_map: dict) -> str:
    """
    執行一次 MCP 工具調用

    參數：
      tool_name: （如 "notion_search"）
      arguments: 
      tool_map: get_tools_for_servers

    回傳：
      工具執行結果文本
    """
    if tool_name not in tool_map:
        return f"錯誤：未知工具 {tool_name}"

    server_info = tool_map[tool_name]
    url = server_info["url"]
    transport = server_info["transport"]
    server_name = server_info.get("server_name", url)

    print(f"🔨 呼叫工具 [{server_name}]: {tool_name}({json.dumps(arguments, ensure_ascii=False)[:200]})")

    try:
        if transport == "sse":
            async with sse_client(url) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.call_tool(tool_name, arguments)
                    return _format_tool_result(result)
        else:
            # streamable_http
            async with streamablehttp_client(url) as (read_stream, write_stream, _):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.call_tool(tool_name, arguments)
                    return _format_tool_result(result)

    except Exception as e:
        error_msg = f"工具呼叫失敗 [{tool_name}]: {str(e)}"
        print(f"❌ {error_msg}")
        return error_msg


def _format_tool_result(result) -> str:
    """将 MCP CallToolResult 格式化為文字"""
    if not result or not result.content:
        return "(空結果)"

    parts = []
    for block in result.content:
        if hasattr(block, "text"):
            parts.append(block.text)
        elif hasattr(block, "data"):
            parts.append(f"[二進位數據: {getattr(block, 'mimeType', 'unknown')}]")
        else:
            parts.append(str(block))

    return "\n".join(parts)


# ============================================================
# 批量工具调用（同服务器复用连接，跨服务器并发）
# ============================================================

async def call_tools_batch(calls: list, tool_map: dict) -> dict:
    """
    批量執行 MCP 工具呼叫。
    - 同一伺服器的多個呼叫複用同一個連接（省去重複握手）
    - 不同伺服器的呼叫透過 asyncio.gather 並發執行

    參數:
      calls: [{"id": "...", "name": "...", "args": {...}}, ...]
      tool_map: { tool_name: {"url": ..., "transport": ..., "server_name": ...} }

    回傳:
      { call_id: result_text }
    """
    if not calls:
        return {}

    # 按服务器 URL 分组
    server_groups = {}
    for c in calls:
        info = tool_map.get(c["name"], {})
        url = info.get("url", "")
        if not url:
            continue
        if url not in server_groups:
            server_groups[url] = {
                "transport": info.get("transport", "streamable_http"),
                "name": info.get("server_name", url),
                "calls": [],
            }
        server_groups[url]["calls"].append(c)

    results = {}

    async def _run_batch(url, transport, server_name, batch_calls):
        """同一伺服器的工具呼叫：只建一次連接，順序執行"""
        try:
            if transport == "sse":
                async with sse_client(url) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        for c in batch_calls:
                            try:
                                result = await session.call_tool(c["name"], c["args"])
                                results[c["id"]] = _format_tool_result(result)
                            except Exception as e:
                                results[c["id"]] = f"工具呼叫失敗 [{c['name']}]: {e}"
            else:
                async with streamablehttp_client(url) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        for c in batch_calls:
                            try:
                                result = await session.call_tool(c["name"], c["args"])
                                results[c["id"]] = _format_tool_result(result)
                            except Exception as e:
                                results[c["id"]] = f"工具呼叫失敗 [{c['name']}]: {e}"

            print(f"🔧 MCP [{server_name}]: {len(batch_calls)} 个工具呼叫完成 ✓")

        except Exception as e:
            print(f"❌ MCP [{server_name}] 連線失敗: {e}")
            for c in batch_calls:
                if c["id"] not in results:
                    results[c["id"]] = f"伺服器連線失敗: {e}"

    # 不同服务器并发执行
    tasks = [
        _run_batch(url, g["transport"], g["name"], g["calls"])
        for url, g in server_groups.items()
    ]
    if tasks:
        await asyncio.gather(*tasks)

    # 填充未知工具的结果
    for c in calls:
        if c["id"] not in results:
            results[c["id"]] = f"錯誤：未知工具 {c['name']}"

    return results
