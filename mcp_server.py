"""
MCP Server — kiwi-mem 記憶系統的 MCP 介面層
==========================================================
依功能域拆分為獨立模組，客戶端只連需要的模組，不用的不佔 token。

模块一：记忆碎片（/memory/mcp）— 6 个工具
  search_memory, save_memory, get_recent, trigger_digest, lock_memory, unlock_memory

模块二：日曆 + Dream（/calendar/mcp）— 11 個工具
  get_day_page, get_calendar_range, save_calendar_page,
  get_comments, add_comment,
  get_user_profile,
  trigger_dream, get_dream_status, get_dream_history, get_dream_scenes, stop_dream

部署方式：掛載到 FastAPI 主應用，共用同一個程序和連接埠。
薄包裝層：不直接碰資料庫，透過 HTTP 呼叫網關本身的 API。
"""

import os
import json
import httpx
from mcp.server.fastmcp import FastMCP

# ============================================================
# 配置
# ============================================================

GATEWAY_PORT = int(os.getenv("PORT", "8080"))
GATEWAY_BASE = f"http://127.0.0.1:{GATEWAY_PORT}"
MCP_AUTH_TOKEN = os.getenv("MCP_AUTH_TOKEN", "")

# 内部调用网关 API 时需要带上 ACCESS_TOKEN（认证中间件会检查）
_access_token = os.getenv("ACCESS_TOKEN", "")
GATEWAY_HEADERS = {"Authorization": f"Bearer {_access_token}"} if _access_token else {}


# ============================================================
# 模块一：记忆碎片
# ============================================================

mcp_memory = FastMCP("Memory Garden", stateless_http=True)


@mcp_memory.tool()
async def search_memory(query: str, limit: int = 10) -> str:
    """
    [category: memory]

    搜尋記憶 — 用自然語言描述你想找的內容，向量語意搜尋會回傳最相關的記憶。

    參數：
    - query: 搜尋關鍵字或自然語言描述，例如"使用者的健康記錄"、"上週聊了什麼"
    - limit: 回傳條數上限（預設10，最大50）

    回傳符合的記憶列表，每條包含標題、內容、重要性、日期。
    """
    if limit > 50:
        limit = 50

    try:
        async with httpx.AsyncClient(timeout=15, headers=GATEWAY_HEADERS) as client:
            resp = await client.get(
                f"{GATEWAY_BASE}/debug/memories",
                params={"q": query, "limit": limit},
            )
            data = resp.json()

        if "error" in data:
            return f"搜尋失敗：{data['error']}"

        # /debug/memories 回傳字段是 memories；保留对旧版 results 的兼容
        results = data.get("memories") or data.get("results", [])
        if not results:
            return f"沒有找到與「{query}」相關的記憶。"

        lines = [f"找到 {len(results)} 條相關記憶（共 {data.get('total_memories', '?')} 條）：\n"]
        for i, mem in enumerate(results, 1):
            title = mem.get("title", "")
            title_tag = f"【{title}】" if title else ""
            date = mem.get("created_at", "")[:10]
            importance = mem.get("importance", "?")
            memory_type = mem.get("memory_type", "fragment")
            content = mem.get("content", "")

            lines.append(
                f"{i}. [{date}] {title_tag}{content}\n"
                f"   重要度: {importance} | 類型: {memory_type}"
            )

        return "\n".join(lines)

    except Exception as e:
        return f"搜尋出錯：{str(e)}"


@mcp_memory.tool()
async def save_memory(content: str, title: str = "", importance: int = 5) -> str:
    """
    [category: memory]

    保存一條新記憶到記憶庫。

    參數：
    - content: 記憶內容（必填），例如"用戶今天搬到了新城市"
    - title: 標題（可選，4-10字概括），如"台灣搬家"
    - importance: 重要度 1-10（預設5），日常瑣事1-4，重要事件5-6，關鍵轉折7-8，核心記憶9-10

    記憶保存後會自動生成向量，可以被語意搜尋找到。
    """
    if not content.strip():
        return "內容不能為空。 "

    if importance < 1:
        importance = 1
    elif importance > 10:
        importance = 10

    try:
        async with httpx.AsyncClient(timeout=15, headers=GATEWAY_HEADERS) as client:
            resp = await client.post(
                f"{GATEWAY_BASE}/debug/memories",
                json={
                    "content": content.strip(),
                    "title": title.strip(),
                    "importance": importance,
                },
            )
            data = resp.json()

        if "error" in data:
            return f"保存失敗：{data['error']}"

        total = data.get("total", "?")
        title_tag = f"【{title}】" if title else ""
        return f"✅ 記憶已保存：{title_tag}{content[:60]}...\n重要度: {importance} | 記憶總數: {total}"

    except Exception as e:
        return f"保存出錯：{str(e)}"


@mcp_memory.tool()
async def get_recent(limit: int = 20) -> str:
    """
    [category: memory]

    取得最近的記憶，按時間倒序排列。

    參數：
    - limit: 回傳條數（預設20，最大50）

    用於快速了解最近發生了什麼、最近聊了什麼。
    """
    if limit > 50:
        limit = 50

    try:
        async with httpx.AsyncClient(timeout=15, headers=GATEWAY_HEADERS) as client:
            resp = await client.get(
                f"{GATEWAY_BASE}/debug/memories",
                params={"limit": limit},
            )
            data = resp.json()

        if "error" in data:
            return f"獲取失敗：{data['error']}"

        # /debug/memories 回傳字段是 memories；保留对旧版 results 的兼容
        results = data.get("memories") or data.get("results", [])
        if not results:
            return "記憶庫為空。"

        lines = [f"最近 {len(results)} 條記憶（共 {data.get('total_memories', '?')} 條）：\n"]
        for i, mem in enumerate(results, 1):
            title = mem.get("title", "")
            title_tag = f"【{title}】" if title else ""
            date = mem.get("created_at", "")[:10]
            content = mem.get("content", "")

            lines.append(f"{i}. [{date}] {title_tag}{content[:80]}")

        return "\n".join(lines)

    except Exception as e:
        return f"取得出錯：{str(e)}"


@mcp_memory.tool()
async def trigger_digest(date: str = "") -> str:
    """
    [category: system_internal]

    手動觸發每日記憶整理 — 把當天的片段記憶合併成獨立事件條目。

    參數：
    - date: 要整理的日期，格式 YYYY-MM-DD（預設整理昨天的）

    通常不需要手動調用，系統每天凌晨自動執行。 
    只在需要立即整理時使用。
    """
    try:
        params = {}
        if date.strip():
            params["date"] = date.strip()

        async with httpx.AsyncClient(timeout=30, headers=GATEWAY_HEADERS) as client:
            resp = await client.get(
                f"{GATEWAY_BASE}/admin/daily-digest",
                params=params,
            )
            data = resp.json()

        if "error" in data:
            return f"整理失敗：{data['error']}"

        return f"✅ 每日整理完成：{json.dumps(data, ensure_ascii=False, indent=2)}"

    except Exception as e:
        return f"整理出錯：{str(e)}"


@mcp_memory.tool()
async def lock_memory(memory_id: int) -> str:
    """
    [category: memory]

    鎖定一條記憶 — 鎖定後熱度永遠為 1.0，不會衰減遺忘，每次聊天都會注入。

    參數：
    - memory_id: 記憶 ID（從搜尋結果中取得）

    用於標記核心記憶，例如重要的個人資訊、關鍵決定、重要約定。
    """
    try:
        async with httpx.AsyncClient(timeout=10, headers=GATEWAY_HEADERS) as client:
            resp = await client.post(
                f"{GATEWAY_BASE}/debug/memories/batch-update",
                json={"ids": [memory_id], "is_permanent": True},
            )
            data = resp.json()

        if "error" in data:
            return f"鎖定失敗：{data['error']}"

        return f"🔒 記憶 #{memory_id} 已鎖定（永不遺忘）"

    except Exception as e:
        return f"鎖定出錯：{str(e)}"


@mcp_memory.tool()
async def unlock_memory(memory_id: int) -> str:
    """
    [category: memory]

    解鎖一條記憶 — 解鎖後恢復正常熱度衰減。

    參數：
    - memory_id: 記憶 ID

    用於取消之前鎖定的記憶，讓它回到正常的遺忘曲線。
    """
    try:
        async with httpx.AsyncClient(timeout=10, headers=GATEWAY_HEADERS) as client:
            resp = await client.post(
                f"{GATEWAY_BASE}/debug/memories/batch-update",
                json={"ids": [memory_id], "is_permanent": False},
            )
            data = resp.json()

        if "error" in data:
            return f"解鎖失敗：{data['error']}"

        return f"🔓 記憶 #{memory_id} 已解鎖（恢復正常遺忘曲線）"

    except Exception as e:
        return f"解鎖出錯：{str(e)}"


# ============================================================
# 模块二：日历 + Dream
# ============================================================

mcp_calendar = FastMCP("Calendar & Dream", stateless_http=True)


# ---- 日历页面 ----

@mcp_calendar.tool()
async def get_day_page(date: str, type: str = "day") -> str:
    """
    [category: calendar]

    查看某一天的日曆頁面（日記/週總結/月總結等）。

    参數：
    - date: 日期，格式 YYYY-MM-DD，如 "2026-04-14"
    - type: 頁面類型，可選 day/week/month/quarter/year（預設 day）

    回傳這一天的標題、內容摘要、時段詳情和 AI 日記。
    """
    if not date.strip():
        return "請提供日期，格式 YYYY-MM-DD"

    try:
        async with httpx.AsyncClient(timeout=15, headers=GATEWAY_HEADERS) as client:
            resp = await client.get(
                f"{GATEWAY_BASE}/calendar/{date.strip()}",
                params={"type": type},
            )
            data = resp.json()

        if "error" in data:
            return f"讀取出錯：{data['error']}"

        page = data.get("page")
        if not page:
            return f"沒有找到 {date} 的{type}頁面。"

        title = page.get("title", "")
        summary = page.get("summary", "")
        sections = page.get("sections") or []
        diary = page.get("diary", "")
        keywords = page.get("keywords") or []

        lines = [f"📅 {date} 的{type}頁面"]
        if title:
            lines[0] += f" — {title}"
        lines.append("")

        if summary:
            lines.append(f"【概要】{summary}\n")

        if isinstance(sections, list):
            for sec in sections:
                period = sec.get("period", "")
                sec_title = sec.get("title", "")
                content = sec.get("content", "")
                lines.append(f"**{period} — {sec_title}**\n{content}\n")

        if diary:
            lines.append(f"📝 AI 的日記：{diary}")

        if keywords:
            kw = "、".join(keywords[:15]) if isinstance(keywords, list) else str(keywords)
            lines.append(f"\n🏷 關鍵字：{kw}")

        return "\n".join(lines)

    except Exception as e:
        return f"讀取出錯：{str(e)}"


@mcp_calendar.tool()
async def get_calendar_range(start: str, end: str, type: str = "") -> str:
    """
    [category: calendar]

    查看一段时间内的日历页面列表。

    参数：
    - start: 開始日期，格式 YYYY-MM-DD
    - end: 結束日期，格式 YYYY-MM-DD
    - type: 過濾類型（可選），day/week/month/quarter/year，留空回傳所有類型

    回傳每個頁面的日期、類型、標題和關鍵字概覽。
    """
    if not start.strip() or not end.strip():
        return "請提供起止日期，格式 YYYY-MM-DD"

    try:
        params = {"start": start.strip(), "end": end.strip()}
        if type.strip():
            params["type"] = type.strip()

        async with httpx.AsyncClient(timeout=15, headers=GATEWAY_HEADERS) as client:
            resp = await client.get(f"{GATEWAY_BASE}/calendar", params=params)
            data = resp.json()

        if "error" in data:
            return f"查詢失敗：{data['error']}"

        pages = data.get("pages", [])
        if not pages:
            return f"{start} ~ {end} 沒有日曆頁面。"

        lines = [f"📅 {start} ~ {end} 共 {len(pages)} 個頁面：\n"]
        for p in pages:
            d = p.get("date", "")
            t = p.get("type", "day")
            title = p.get("title", "")
            kw = p.get("keywords") or []
            summary = p.get("summary", "")

            label = f"[{d}] ({t})"
            if title:
                label += f" {title}"
            if kw:
                kw_str = "、".join(kw[:8]) if isinstance(kw, list) else str(kw)
                label += f" | {kw_str}"
            elif summary:
                label += f" | {summary[:60]}"
            lines.append(label)

        return "\n".join(lines)

    except Exception as e:
        return f"查詢出錯：{str(e)}"


@mcp_calendar.tool()
async def save_calendar_page(date: str, content: str, title: str = "", type: str = "day") -> str:
    """
    [category: calendar]

    寫入或更新日曆頁面（日記）。

    参数：
    - date: 日期，格式 YYYY-MM-DD
    - content: 正文内容（Markdown 格式）
    - title: 標題（可選）
    - type: 頁面類型，day/week/month/quarter/year（默認 day）

    用於 AI 在對話中為使用者寫日記、補充週記等。
    """
    if not date.strip():
        return "請提供日期，格式 YYYY-MM-DD"
    if not content.strip():
        return "內容不能為空。"

    try:
        async with httpx.AsyncClient(timeout=15, headers=GATEWAY_HEADERS) as client:
            resp = await client.put(
                f"{GATEWAY_BASE}/admin/calendar/{date.strip()}",
                json={
                    "content": content.strip(),
                    "title": title.strip(),
                    "type": type.strip(),
                },
            )
            data = resp.json()

        if "error" in data:
            return f"保存失敗：{data['error']}"

        page_id = data.get("id", "?")
        return f"✅ 日曆頁面已儲存：{date}（{type}）| ID: {page_id}"

    except Exception as e:
        return f"保存出錯：{str(e)}"


# ---- 评论 ----

@mcp_calendar.tool()
async def get_comments(target_type: str, target_id: int) -> str:
    """
    [category: calendar]

    讀取某個頁面的評論清單。

    参数：
    - target_type: 目標類型，如 "day_page"、"scene"
    - target_id: 目標 ID（日曆頁面的 ID 或場景的 ID）

    回傳該頁面下的所有評論。
    """
    try:
        async with httpx.AsyncClient(timeout=10, headers=GATEWAY_HEADERS) as client:
            resp = await client.get(
                f"{GATEWAY_BASE}/comments",
                params={"target_type": target_type, "target_id": target_id},
            )
            data = resp.json()

        if "error" in data:
            return f"讀取失敗：{data['error']}"

        comments = data.get("comments", [])
        if not comments:
            return "暫無評論。"

        lines = [f"💬 共 {len(comments)} 條評論：\n"]
        for c in comments:
            author = c.get("author", "?")
            content = c.get("content", "")
            time = str(c.get("created_at", ""))[:16]
            cid = c.get("id", "?")
            parent = c.get("parent_id")
            prefix = f"  ↳ 回复 #{parent} " if parent else ""
            lines.append(f"#{cid} [{time}] {prefix}{author}：{content}")

        return "\n".join(lines)

    except Exception as e:
        return f"讀取出錯：{str(e)}"


@mcp_calendar.tool()
async def add_comment(target_type: str, target_id: int, content: str, parent_id: int = 0) -> str:
    """
    [category: calendar]

    在日历页面或场景下添加评论。

    参数：
    - target_type: 目標類型，如 "day_page"、"scene"
    - target_id: 目標 ID
    - content: 評論內容
    - parent_id: 回覆的評論 ID（0 表示頂層評論）

    AI 可以用這個工具在日記下方寫備註、標記或補充。
    """
    if not content.strip():
        return "評論內容不能為空。"

    try:
        body = {
            "target_type": target_type,
            "target_id": target_id,
            "content": content.strip(),
            "author": "assistant",
        }
        if parent_id > 0:
            body["parent_id"] = parent_id

        async with httpx.AsyncClient(timeout=10, headers=GATEWAY_HEADERS) as client:
            resp = await client.post(
                f"{GATEWAY_BASE}/comments",
                json=body,
            )
            data = resp.json()

        if "error" in data:
            return f"評論失敗：{data['error']}"

        comment = data.get("comment", {})
        cid = comment.get("id", "?")
        return f"✅ 評論已發布（#{cid}）"

    except Exception as e:
        return f"評論出錯：{str(e)}"


# ---- 用户画像 ----

@mcp_calendar.tool()
async def get_user_profile() -> str:
    """
    [category: profile]

    查看當前的使用者畫像 — AI 對使用者的認知。

    畫像包含四個板塊：基本檔案、洞察、近期重點、長期偏好。 
    由每日整理自動更新，也可手動觸發更新。
    """
    try:
        async with httpx.AsyncClient(timeout=10, headers=GATEWAY_HEADERS) as client:
            resp = await client.get(f"{GATEWAY_BASE}/admin/config")
            data = resp.json()

        profile = data.get("user_profile", {}).get("value", "")
        if not profile:
            return "暫無使用者畫像。"

        return f"🪞 使用者畫像\n\n{profile}"

    except Exception as e:
        return f"讀取出錯：{str(e)}"


# ---- Dream ----

@mcp_calendar.tool()
async def trigger_dream() -> str:
    """
    [category: dream]

    讓 AI 去睡覺（觸發 Dream 記憶整合）。

    Dream 會整理片段記憶、形成記憶場景（MemScene）、生成前瞻訊號（Foresight）。 
    通常在碎片堆積較多或長時間未整理時使用。
    """
    try:
        # /dream/start 回傳 SSE 流（StreamingResponse），Dream 实际跑 1-5 分钟。
        # 不能用 client.post() 等响应完整 —— httpx 默认会把整个流读完才回傳，
        # timeout 设多大都可能不够；而且客户端断开会触发 FastAPI 端 generator 的
        # CancelledError 把 Dream 中途杀掉。
        # 正确做法：用 client.stream() 读到第一个 data: 事件就 return，
        # 后续 Dream 在网关后台继续跑，让客户端用 get_dream_status 查进度。
        async with httpx.AsyncClient(timeout=60, headers=GATEWAY_HEADERS) as client:
            async with client.stream(
                "POST",
                f"{GATEWAY_BASE}/dream/start",
                json={"trigger_type": "manual"},
            ) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode("utf-8", errors="ignore")[:300]
                    return f"Dream 啟動失敗（HTTP {resp.status_code}）：{body}"

                first_data = ""
                async for line in resp.aiter_lines():
                    line = (line or "").strip()
                    if line.startswith("data:"):
                        first_data = line[len("data:"):].strip()
                        break

        if not first_data:
            return "🌙 Dream 已啟動，可以用 get_dream_status 查看進度。"

        # 错误事件
        if first_data.startswith("{") and '"error"' in first_data.lower():
            return f"Dream 啟動失敗：{first_data[:200]}"

        return f"🌙 Dream 已啟動：{first_data[:200]}\n後續可用 get_dream_status 查看進度。 "

    except httpx.TimeoutException:
        # 60s 内连首个事件都没到, 但请求已发出, Dream 多半已在后台跑了
        return "🌙 Dream 已啟動（首事件逾時未到，可用 get_dream_status 確認）"
    except Exception as e:
        return f"啟動出錯：{type(e).__name__}: {e}"


@mcp_calendar.tool()
async def stop_dream() -> str:
    """
    [category: dream]

    中斷正在進行的 Dream。

    用於在 Dream 過程中需要緊急打斷時使用。
    """
    try:
        async with httpx.AsyncClient(timeout=10, headers=GATEWAY_HEADERS) as client:
            resp = await client.post(f"{GATEWAY_BASE}/dream/stop")
            data = resp.json()

        if "error" in data:
            return f"中斷失敗：{data['error']}"

        return f"⏹ Dream 已中斷：{json.dumps(data, ensure_ascii=False)}"

    except Exception as e:
        return f"中斷出錯：{str(e)}"


@mcp_calendar.tool()
async def get_dream_status() -> str:
    """
    [category: dream]

    看看 Dream 狀態 — 是否正在做夢、上次做夢的結果、待處理碎片數量。
    """
    try:
        async with httpx.AsyncClient(timeout=10, headers=GATEWAY_HEADERS) as client:
            resp = await client.get(f"{GATEWAY_BASE}/dream/status")
            data = resp.json()

        is_running = data.get("is_running", False)
        last = data.get("last_completed")

        lines = []
        if is_running:
            lines.append("🌙 AI Dreaming.")
            current = data.get("current", {})
            if current:
                lines.append(f"   Dream #{current.get('id', '?')} | 开始于 {str(current.get('started_at', ''))[:19]}")
        else:
            lines.append("😴 AI 目前醒着。")

        # 待处理碎片
        unprocessed = data.get("unprocessed_count", 0)
        drowsy = data.get("is_drowsy", False)
        if unprocessed > 0:
            drowsy_tag = "（已犯困，建議Dream）" if drowsy else ""
            lines.append(f"   待處理碎片：{unprocessed} 條{drowsy_tag}")

        if last:
            lines.append(f"\n上次 Dream：#{last.get('id', '?')}")
            lines.append(f"   時間：{str(last.get('started_at', ''))[:19]} → {str(last.get('finished_at', ''))[:19]}")
            lines.append(f"   處理碎片：{last.get('memories_processed', 0)} 條")
            lines.append(f"   刪除：{last.get('memories_deleted', 0)} | 合併：{last.get('memories_merged', 0)}")
            lines.append(f"   新建場景：{last.get('scenes_created', 0)} | 前瞻訊號：{last.get('foresights_generated', 0)}")

        return "\n".join(lines) if lines else "暫無 Dream 記錄。"

    except Exception as e:
        return f"查詢出錯：{str(e)}"


@mcp_calendar.tool()
async def get_dream_history(limit: int = 10) -> str:
    """
    [category: dream]

    查看 Dream 執行歷史記錄。

    参数：
    - limit: 回傳條數（預設10）

    顯示每次 Dream 的時間、處理碎片數、新場景數等。
    """
    if limit > 50:
        limit = 50

    try:
        async with httpx.AsyncClient(timeout=10, headers=GATEWAY_HEADERS) as client:
            resp = await client.get(
                f"{GATEWAY_BASE}/dream/history",
                params={"limit": limit},
            )
            data = resp.json()

        if "error" in data:
            return f"查詢失敗：{data['error']}"

        history = data.get("history", [])
        if not history:
            return "還沒有 Dream 記錄。"

        lines = [f"🌙 Dream 歷史（最近 {len(history)} 次）：\n"]
        for h in history:
            did = h.get("id", "?")
            status = h.get("status", "?")
            started = str(h.get("started_at", ""))[:16]
            finished = str(h.get("finished_at", ""))[:16]
            processed = h.get("memories_processed", 0)
            deleted = h.get("memories_deleted", 0)
            merged = h.get("memories_merged", 0)
            scenes = h.get("scenes_created", 0)
            foresights = h.get("foresights_generated", 0)

            status_icon = {"completed": "✅", "running": "🔄", "interrupted": "⏹", "failed": "❌"}.get(status, "❓")
            lines.append(
                f"{status_icon} Dream #{did} | {started} → {finished}\n"
                f"   碎片: {processed} | 刪除: {deleted} | 合併: {merged} | 場景: {scenes} | 前瞻: {foresights}"
            )

        return "\n".join(lines)

    except Exception as e:
        return f"查詢出錯：{str(e)}"


@mcp_calendar.tool()
async def get_dream_scenes() -> str:
    """
    [category: dream]

    查看所有活躍的記憶場景（MemScene）。

    記憶場景是 Dream 過程中將相關片段記憶凝聚成的主題敘事。 
    每個場景包含標題、敘事文字和前瞻性訊號（Foresight）。
    """
    try:
        async with httpx.AsyncClient(timeout=10, headers=GATEWAY_HEADERS) as client:
            resp = await client.get(f"{GATEWAY_BASE}/dream/scenes")
            data = resp.json()

        if "error" in data:
            return f"查詢失敗：{data['error']}"

        scenes = data.get("scenes", [])
        if not scenes:
            return "還沒有記憶場景。"

        lines = [f"🎭 活躍場景共 {len(scenes)} 個：\n"]
        for s in scenes:
            sid = s.get("id", "?")
            title = s.get("title", "無標題")
            narrative = s.get("narrative", "")
            foresight = s.get("foresight") or []
            created = str(s.get("created_at", ""))[:10]
            memory_count = s.get("memory_count", 0)

            lines.append(f"🎬 #{sid}「{title}」({created}，{memory_count} 條碎片)")
            if narrative:
                lines.append(f"   {narrative[:120]}{'…' if len(narrative) > 120 else ''}")
            if foresight:
                fs_list = foresight if isinstance(foresight, list) else [foresight]
                for f in fs_list[:3]:
                    lines.append(f"   🔮 {f}")
            lines.append("")

        return "\n".join(lines)

    except Exception as e:
        return f"查詢出錯：{str(e)}"


# ============================================================
# 获取 ASGI app（用于挂载到 FastAPI）
# ============================================================

def get_memory_mcp_app():
    """
    記憶碎片模組 MCP。
    掛載路徑：/memory → URL：/memory/mcp
    6 個工具：search_memory, save_memory, get_recent, trigger_digest, lock_memory, unlock_memory
    """
    return mcp_memory.streamable_http_app()


def get_calendar_mcp_app():
    """
    日曆 + Dream 模組 MCP。
    掛載路徑：/calendar → URL：/calendar/mcp
    11 個工具：get_day_page, get_calendar_range, save_calendar_page,
              get_comments, add_comment, get_user_profile,
              trigger_dream, stop_dream, get_dream_status, get_dream_history, get_dream_scenes
    """
    return mcp_calendar.streamable_http_app()


# 向后兼容（旧代码 import 用）
mcp = mcp_memory

def get_mcp_app():
    """向後相容：回傳記憶模組"""
    return get_memory_mcp_app()
