"""
每日記憶整理模組 —— 每天自動把片段記憶合併為事件條目
================================================================
每天東八區 0:00 觸發一次，讀取前一天的碎片記憶（memory_type='fragment'），
讓 Haiku 按事件主題合併成獨立條目（memory_type='daily_digest'），
合併後的碎片標記為 'digested'，不再參與日常注入。

v1.0 初版
"""

import os
import json
import asyncio
import httpx
from datetime import datetime, timedelta, timezone

# ============================================================
# API 配置 —— 记忆整理用独立 key，避免和聊天抢额度
# ============================================================

MEMORY_API_KEY = os.getenv("MEMORY_API_KEY", "") or os.getenv("API_KEY", "")
_RAW_BASE_URL = os.getenv("MEMORY_API_BASE_URL", "") or os.getenv("API_BASE_URL", "https://openrouter.ai/api/v1/chat/completions")

# 确保 URL 以 /chat/completions 结尾
MEMORY_API_BASE_URL = _RAW_BASE_URL if _RAW_BASE_URL.rstrip("/").endswith("/chat/completions") else f"{_RAW_BASE_URL.rstrip('/')}/chat/completions"

DIGEST_MODEL = os.getenv("MEMORY_MODEL", "anthropic/claude-haiku-4")

# 东八区（北京 / 上海 / 台北）
TZ_CST = timezone(timedelta(hours=8))

# ============================================================
# 整理 Prompt
# ============================================================

DIGEST_PROMPT = """你是記憶整理專家。以下是使用者在 {date} 這一天的片段記憶，請將它們按事件主題合併整理。

## 整理規則
- 依主題分類合併（如"前端開發""飲食紀錄""情緒狀態""作息""角色扮演""理財"等）
- 每條都是獨立事件，不要把不相關的事硬合在一起
- 保留關鍵細節（時間、數值、具體內容），去除重複和瑣碎的部分
- 如果某塊碎片本身已經很完整獨立，保持原樣即可
- 標題以 4-10 個字概括主題
- 內容用 1-3 句話總結這個事件的要點
- importance 根據事件對使用者的重要程度評分：9-10 核心事件 / 7-8 重要 / 5-6 普通

## 可用的分类列表
{categories_list}

## 今天的碎片记忆
{fragments}

## 輸出格式
只輸出 JSON 數組，不要其他內容：
[
  {"title": "簡短標題", "content": "整理後的內容", "importance": 7, "category": "分類名"},
  {"title": "簡短標題", "content": "整理後的內容", "importance": 5, "category": "分類名"}
]

category 欄位從上面的分類清單中選擇最合適的一個，如果都不合適就填空字串。 """

# 防止同一日期被并发整理（定时器 + 手动 API 同时触发）
_digest_running: set = set()
_digest_lock = asyncio.Lock()


async def run_daily_digest(target_date: str = None, model_override: str = None, prompt_override: str = None):
    """
    執行每日記憶整理

    Args:
        target_date: 要整理的日期，格式 "2026-03-02"，預設為昨天
        model_override: 覆蓋預設整理模型
        prompt_override: 覆蓋預設整理提示詞
    """
    from database import get_pool, save_memory, get_embedding, get_all_categories, match_category_by_name
    from datetime import date as date_cls

    now_cst = datetime.now(TZ_CST)

    if target_date:
        # 校验格式，避免后续 fromisoformat 直接抛 ValueError 让接口 500
        try:
            date_cls.fromisoformat(target_date)
        except (ValueError, TypeError):
            return {"error": f"無效日期格式: {target_date!r}，需要 YYYY-MM-DD"}
        date_str = target_date
    else:
        yesterday = now_cst - timedelta(days=1)
        date_str = yesterday.strftime("%Y-%m-%d")

    # 防止同一日期被并发整理（定时器 + 手动触发可能同时进来）
    async with _digest_lock:
        if date_str in _digest_running:
            print(f"⚠️ {date_str} 正在整理中，跳過重複請求")
            return {"date": date_str, "fragments": 0, "digests": 0, "skipped": "already running"}
        _digest_running.add(date_str)
    try:
        return await _run_daily_digest_impl(date_str, now_cst, model_override, prompt_override)
    finally:
        _digest_running.discard(date_str)


async def _run_daily_digest_impl(date_str: str, now_cst, model_override: str = None, prompt_override: str = None):
    """實際執行每日整理（由 run_daily_digest 調用，已有並發保護）"""
    from database import get_pool, save_memory, get_embedding, get_all_categories, match_category_by_name

    print(f"\n🌙 開始每日記憶整理：{date_str}")
    print(f"   當前時間（東八區）：{now_cst.strftime('%Y-%m-%d %H:%M:%S')}")
    
    # 将日期字符串转为 date 对象（asyncpg 需要 date 对象而非字符串）
    from datetime import date as date_cls
    target_date_obj = date_cls.fromisoformat(date_str)
    
    # 获取分类列表
    try:
        all_cats = await get_all_categories()
        cat_names = [c["name"] for c in all_cats]
        categories_text = "、".join(cat_names) if cat_names else "（暫無分類，category 欄位填空字串即可）"
    except Exception:
        cat_names = []
        categories_text = "（暫無分類，category 欄位填空字串即可）"
    
    # ---- 1. 查询当天的碎片记忆 ----
    pool = await get_pool()
    async with pool.acquire() as conn:
        fragments = await conn.fetch("""
            SELECT id, title, content, importance, created_at
            FROM memories
            WHERE COALESCE(memory_type, 'fragment') = 'fragment'
              AND (created_at AT TIME ZONE 'Asia/Shanghai')::date = $1
            ORDER BY created_at ASC
        """, target_date_obj)
    
    if not fragments:
        print(f"   📭 {date_str} 沒有碎片記憶，跳過整理")
        return {"date": date_str, "fragments": 0, "digests": 0}
    
    print(f"   📋 找到 {len(fragments)} 條碎片記憶")
    
    # 如果只有 1-2 條，不值得合并，直接标记为 digest
    if len(fragments) <= 2:
        async with pool.acquire() as conn:
            for f in fragments:
                await conn.execute(
                    "UPDATE memories SET memory_type = 'daily_digest' WHERE id = $1",
                    f["id"]
                )
        print(f"   ✅ 碎片太少，直接升級為日誌條目")
        return {"date": date_str, "fragments": len(fragments), "digests": len(fragments)}
    
    # ---- 2. 格式化碎片，发给 Haiku 整理 ----
    fragment_lines = []
    for f in fragments:
        title = f["title"] or ""
        content = f["content"]
        imp = f["importance"]
        if title:
            fragment_lines.append(f"- 【{title}】{content}（重要度:{imp}）")
        else:
            fragment_lines.append(f"- {content}（重要度:{imp}）")
    
    fragments_text = "\n".join(fragment_lines)
    base_prompt = prompt_override if prompt_override else DIGEST_PROMPT
    prompt = base_prompt.replace("{date}", date_str).replace("{fragments}", fragments_text).replace("{categories_list}", categories_text)
    
    # 确定使用的模型
    use_model = model_override if model_override else DIGEST_MODEL
    
    # v5.4：动态解析供应商端点
    try:
        from database import resolve_model_endpoint
        use_api_url, use_api_key, use_api_format = await resolve_model_endpoint(use_model)
    except Exception:
        use_api_url = MEMORY_API_BASE_URL
        use_api_key = MEMORY_API_KEY
        use_api_format = "openai"

    # ---- 3. 調用 Haiku ----
    try:
        from anthropic_adapter import prepare_background_request, parse_background_response
        _body = {
            "model": use_model,
            "max_tokens": 2000,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": f"請整理 {date_str} 的片段記憶。"},
            ],
        }
        _headers, _send_body = prepare_background_request(
            use_api_key, use_api_format, _body,
            referer="https://midsummer-gateway.local",
            title="AI Memory Gateway - Daily Digest",
        )
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post(use_api_url, headers=_headers, json=_send_body)

            if response.status_code != 200:
                print(f"   ⚠️ Haiku 請求失敗: {response.status_code}")
                return {"date": date_str, "fragments": len(fragments), "digests": 0, "error": f"HTTP {response.status_code}"}

            data = parse_background_response(response.json(), use_api_format)
            text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            
            # 日志
            print(f"   🔍 整理模型返回（前200字）: {text[:200]}...")
            
            # 清理 markdown
            text = text.strip()
            if text.startswith("```json"):
                text = text[7:]
            if text.startswith("```"):
                text = text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()
            
            # 解析 JSON（正则兜底）
            digests = None
            try:
                digests = json.loads(text)
            except json.JSONDecodeError:
                import re
                match = re.search(r'\[.*\]', text, re.DOTALL)
                if match:
                    try:
                        digests = json.loads(match.group())
                        print(f"   🔧 JSON 正規兜底解析成功")
                    except json.JSONDecodeError:
                        pass
            
            if not digests or not isinstance(digests, list):
                print(f"   ⚠️ 整理模型回傳格式錯誤")
                return {"date": date_str, "fragments": len(fragments), "digests": 0, "error": "invalid format"}
    
    except Exception as e:
        print(f"   ⚠️ 每日整理出錯: {e}")
        return {"date": date_str, "fragments": len(fragments), "digests": 0, "error": str(e)}
    
    # ---- 4. 存储整理后的事件條目 ----
    saved_count = 0
    for d in digests:
        if not isinstance(d, dict) or "content" not in d:
            continue
        
        title = str(d.get("title", ""))
        content = str(d["content"])
        # importance 安全转换：LLM 可能返回浮点、字符串或 null
        try:
            importance = int(float(d.get("importance", 5)))
            importance = max(1, min(10, importance))
        except (ValueError, TypeError):
            importance = 5
        
        # 自动匹配分类
        cat_id = None
        cat_hint = str(d.get("category", ""))
        if cat_hint:
            cat_id = await match_category_by_name(cat_hint)
        
        # 在 content 前面加上日期，方便搜索命中
        content_with_date = f"[{date_str}] {content}"
        
        # 生成 embedding
        embed_text = f"{title} {content_with_date}" if title else content_with_date
        embedding = await get_embedding(embed_text)
        embedding_json = json.dumps(embedding) if embedding else None
        
        # 存入数据库，memory_type = 'daily_digest'
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO memories (content, importance, source_session, embedding, title, memory_type, created_at, category_id, source)
                VALUES ($1, $2, $3, $4, $5, 'daily_digest', $6::timestamptz, $7, 'ai_digest')
            """,
                content_with_date, importance, "daily_digest", embedding_json, title,
                f"{date_str}T00:00:00+08:00", cat_id
            )
        
        saved_count += 1
        print(f"   📌 [{title}] {content_with_date[:60]}...")
    
    # ---- 5. 把原始碎片标记为已整理 ----
    async with pool.acquire() as conn:
        fragment_ids = [f["id"] for f in fragments]
        await conn.execute("""
            UPDATE memories SET memory_type = 'digested' 
            WHERE id = ANY($1::int[])
        """, fragment_ids)
    
    print(f"   ✅ 整理完成：{len(fragments)} 條碎片 → {saved_count} 條事件")
    print(f"   ✅ 已將 {len(fragments)} 條碎片標示為 digested")
    
    return {"date": date_str, "fragments": len(fragments), "digests": saved_count}


# ============================================================
# 用户画像更新 —— 每日整理后自动調用
# ============================================================

DEFAULT_PROFILE_PROMPT = """你是使用者畫像維護專家。根據今天的對話日誌，增量更新使用者畫像。

## ⚠️ 背景說明（重要）

你看到的對話日誌是**使用者與 AI 助理之間的對話**，不是使用者與真人的對話。
- 對話中 role=user 的訊息來自用戶，role=assistant 的訊息來自 AI 助手
- 使用者可能對 AI 使用親暱稱呼，這不代表現實人際關係
- 只擷取關於**使用者本身**的資訊（健康、偏好、生活狀態等），不要把使用者對 AI 的互動方式誤解為現實人際關係

## 畫像結構（嚴格遵循）

畫像必須包含以下四個板塊，並以 ## 標題分隔：

### 📌 基本檔案
使用者的穩定事實資訊。很少變化，只在有新資訊時更新。
包括：姓名/暱稱、年齡、身分、健康狀況、用藥方案、居住狀態、家庭關係、寵物等。

### 🔍 Helpful User Insights
與用戶高效互動的實用洞察。關注"怎麼跟這個用戶溝通最好"。
包括：溝通偏好（語氣、格式、長度）、思考方式、決策風格、敏感點、容易被什麼打動、哪些話題需要小心、喜歡什麼樣的回應方式。
每條用 - 列出，簡潔但具體，避免泛泛而談。

### 🔥 近期重點話題
用戶最近一兩週在做什麼、關注什麼、聊什麼。這個板塊變化最頻繁。
包括：正在推進的專案/計劃、最近的興趣、正在處理的問題、近期情緒趨勢。
每條用 - 列出，標註大致時間（如"3月底"）。
已完成或不再相關的話題要移除。

### 💡 長期偏好與價值觀
使用者穩定的美感偏好、價值觀、生活態度。比基本檔案更軟性，比 insights 更深層。
包括：世界觀、美感偏好、創作風格、生活理念、對科技/工具的態度。
不常變，只在發現新的穩定偏好時才添加。

## 更新規則

1. 只做增量修改：有新資訊就加/改/刪，沒有就維持不變
2. 過時資訊要刪除（計畫已完成、狀態已改變、話題不再相關）
3. 近期重點話題是變化最快的板塊，每次都要重新檢視
4. Helpful User Insights 只在發現明確的新模式時才添加，不要從單次對話過度推斷
5. 每個板塊控制在 5-15 條，總長度控制在 800 字以內
6. 用中文撰寫，語言簡潔俐落，不要套話
7. 如果今天的日誌沒有值得更新的內容，原樣返回現有畫像
8. 只輸出更新後的畫像全文（四個板塊），不要輸出解釋

## 目前畫像
{current_profile}

## 今天的對話日誌
{today_digest}"""


async def update_user_profile(digest_text: str = None, model_override: str = None, prompt_override: str = None):
    """
    更新使用者畫像（增量式）
    
    Args:
        digest_text: 今天的日誌文本，如果為空則從最近的日頁面讀取
        model_override: 覆蓋預設模型
        prompt_override: 覆蓋預設提示詞
    """
    from config import get_config, set_config
    
    print("\n🪞 開始更新使用者畫像...")
    
    # 1. 读取当前画像
    current_profile = await get_config("user_profile") or ""
    
    # 2. 准备今天的日志（从日页面读取）
    if not digest_text:
        from database import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            # 读最近的日页面
            row = await conn.fetchrow("""
                SELECT sections, diary, date FROM calendar_pages 
                WHERE type = 'day'
                ORDER BY date DESC LIMIT 1
            """)
        if not row:
            print("   📭 沒有找到日頁面，跳過畫像更新")
            return {"status": "skipped", "reason": "no day page"}
        # 把日页面内容格式化为文本
        sections = row["sections"] or []
        parts = [f"日期：{row['date']}"]
        for sec in (sections if isinstance(sections, list) else []):
            period = sec.get("period", "")
            title = sec.get("title", "")
            content = sec.get("content", "")
            parts.append(f"【{period} — {title}】{content}")
        if row.get("diary"):
            parts.append(f"AI 的話：{row['diary']}")
        digest_text = "\n\n".join(parts)
    
    # 3. 确定模型（优先用传入的 > 压缩模型 > 标题模型 > 环境变量）
    use_model = model_override
    if not use_model:
        use_model = await get_config("default_compress_model") or ""
    if not use_model:
        use_model = await get_config("default_title_model") or ""
    if not use_model:
        use_model = DIGEST_MODEL
    
    # 4. 构建 prompt
    base_prompt = prompt_override
    if not base_prompt:
        base_prompt = await get_config("prompt_user_profile") or ""
    if not base_prompt:
        base_prompt = DEFAULT_PROFILE_PROMPT
    
    profile_display = current_profile if current_profile else "（尚無畫像，請根據日誌生成初始版本）"
    prompt = base_prompt.replace("{current_profile}", profile_display).replace("{today_digest}", digest_text)
    
    # 5. 調用模型（v5.4：走供应商路由）
    try:
        from database import resolve_model_endpoint
        use_api_url, use_api_key, use_api_format = await resolve_model_endpoint(use_model)
    except Exception:
        use_api_url = MEMORY_API_BASE_URL
        use_api_key = MEMORY_API_KEY
        use_api_format = "openai"

    try:
        from anthropic_adapter import prepare_background_request, parse_background_response
        _body = {
            "model": use_model,
            "max_tokens": 2000,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": "請根據今天的日誌更新使用者畫像。"},
            ],
        }
        _headers, _send_body = prepare_background_request(
            use_api_key, use_api_format, _body,
            referer="https://midsummer-gateway.local", title="User Profile Update",
        )
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post(use_api_url, headers=_headers, json=_send_body)

            if response.status_code != 200:
                print(f"   ⚠️ 畫像更新請求失敗: {response.status_code}")
                return {"status": "error", "error": f"HTTP {response.status_code}"}

            data = parse_background_response(response.json(), use_api_format)
            new_profile = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            
            if not new_profile:
                print("   ⚠️ 模型返回空內容")
                return {"status": "error", "error": "empty response"}
    
    except Exception as e:
        print(f"   ⚠️ 畫像更新出錯: {e}")
        return {"status": "error", "error": str(e)}
    
    # 6. 保存更新后的画像
    changed = new_profile != current_profile
    await set_config("user_profile", new_profile)
    
    if changed:
        print(f"   ✅ 使用者畫像已更新（{len(new_profile)} 字）")
    else:
        print(f"   ℹ️ 畫像無變化")
    
    return {
        "status": "updated" if changed else "unchanged",
        "length": len(new_profile),
        "changed": changed,
    }


# ============================================================
# 定时調度器 —— 每天东八区 0:05 执行
# ============================================================

async def daily_digest_scheduler():
    """
    後台定時任務，每天東八區 0:05 執行。
    選 0:05 而不是 0:00，給最後一輪對話留個緩衝。
    """
    print("🕐 每日記憶整理調度已啟動（東八區 0:05 觸發）")
    
    while True:
        try:
            now = datetime.now(TZ_CST)

            # 计算下一个 0:05
            tomorrow = now.replace(hour=0, minute=5, second=0, microsecond=0)
            if now >= tomorrow:
                tomorrow += timedelta(days=1)

            # 在 sleep 前就锁定要整理的日期，避免 sleep 因 OS 挂起或时钟跳变后
            # 用 datetime.now() 重算时落到錯误的"昨天"
            target_date_str = (tomorrow - timedelta(days=1)).strftime("%Y-%m-%d")

            wait_seconds = (tomorrow - now).total_seconds()
            hours = int(wait_seconds // 3600)
            mins = int((wait_seconds % 3600) // 60)
            print(f"🕐 下次整理：{tomorrow.strftime('%Y-%m-%d %H:%M')}（{hours}小時{mins}分鐘後），目標日期 {target_date_str}")

            await asyncio.sleep(wait_seconds)

            yesterday = target_date_str
            
            # 1. 日页面生成（从碎片生成详细日页面）
            try:
                page_result = await generate_day_page(yesterday)
                print(f"📅 日頁面生成結果：{page_result}")
            except Exception as e:
                print(f"⚠️ 日頁面生成出錯: {e}")
            
            # 2. 用户画像更新（从日页面读素材）
            try:
                profile_result = await update_user_profile()
                print(f"🪞 畫像更新結果：{profile_result}")
            except Exception as e:
                print(f"⚠️ 畫像更新出錯: {e}")
            
            # 3. 检查是否需要生成周/月/季/年总结
            try:
                await check_and_generate_summaries()
            except Exception as e:
                print(f"⚠️ 總結生成出錯: {e}")
            
            # 4. 场景向量回填 + 锁定退役 + 自动软化（先模糊降级，再清理）
            try:
                scene_backfill_result = await backfill_scene_embeddings()
                print(f"scene embedding backfill result: {scene_backfill_result}")
            except Exception as e:
                print(f"scene embedding backfill failed: {e}")

            try:
                retire_result = await retire_stale_locks()
                print(f"auto lock retire result: {retire_result}")
            except Exception as e:
                print(f"auto lock retire failed: {e}")

            try:
                soften_result = await auto_soften_aging_memories()
                print(f"🫧 自動柔化結果：{soften_result}")
            except Exception as e:
                print(f"⚠️ 自動柔化出錯: {e}")

            # 5. 清理过期碎片
            try:
                cleanup_result = await cleanup_expired_fragments()
                print(f"🧹 碎片清理結果：{cleanup_result}")
            except Exception as e:
                print(f"⚠️ 碎片清理出錯: {e}")
            
        except asyncio.CancelledError:
            print("🕐 每日整理調度器已停止")
            break
        except Exception as e:
            print(f"⚠️ 調度器出錯: {e}，60秒後重試")
            await asyncio.sleep(60)


async def backfill_scene_embeddings(limit: int = 20):
    """Backfill embeddings for active scenes that do not have one yet."""
    try:
        from database import get_pool, get_embedding, build_scene_embedding_text

        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT id, title, atomic_facts
                FROM mem_scenes
                WHERE status = 'active'
                  AND embedding IS NULL
                ORDER BY updated_at DESC
                LIMIT $1
            """, limit)

        if not rows:
            return {"status": "success", "backfilled": 0, "skipped": 0, "candidates": 0}

        backfilled = 0
        skipped = 0
        for row in rows:
            r = dict(row)
            scene_id = r["id"]
            text = build_scene_embedding_text(r.get("title", ""), r.get("atomic_facts"))
            if not text:
                skipped += 1
                continue
            try:
                embedding = await get_embedding(text)
                if embedding is None:
                    skipped += 1
                    print(f"⚠️ 場景 embedding 回填失敗: #{scene_id}")
                    continue
                async with pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE mem_scenes SET embedding = $1::jsonb WHERE id = $2",
                        json.dumps(embedding),
                        scene_id,
                    )
                backfilled += 1
            except Exception as e:
                skipped += 1
                print(f"⚠️ 場景 embedding 回填異常: #{scene_id} {type(e).__name__}: {e}")

        return {
            "status": "success",
            "backfilled": backfilled,
            "skipped": skipped,
            "candidates": len(rows),
        }
    except Exception as e:
        print(f"scene embedding backfill failed: {type(e).__name__}: {e}")
        return {"status": "error", "backfilled": 0, "skipped": 0, "error": str(e)}


async def retire_stale_locks():
    """Retire stale auto-locked memories without deleting them."""
    try:
        from database import get_pool
        from config import get_config_bool, get_config_int

        enabled = await get_config_bool("lock_retire_enabled", True)
        if not enabled:
            print("auto lock retire disabled")
            return {"status": "disabled", "retired": 0}

        retire_days = max(0, await get_config_int("lock_retire_days", 90))
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT id, COALESCE(title, '') as title
                FROM memories
                WHERE is_permanent = TRUE
                  AND lock_source IN ('auto', 'dream')
                  AND last_accessed IS NOT NULL
                  AND last_accessed < NOW() - $1 * INTERVAL '1 day'
                ORDER BY last_accessed ASC
            """, retire_days)

            ids = [row["id"] for row in rows]
            if ids:
                await conn.execute("""
                    UPDATE memories
                    SET is_permanent = FALSE,
                        lock_source = NULL,
                        importance = GREATEST(importance, 8),
                        dream_processed_at = NULL
                    WHERE id = ANY($1::int[])
                """, ids)

        titles = [row["title"] or f"#{row['id']}" for row in rows]
        if titles:
            print(f"auto lock retired {len(titles)} memories: {', '.join(titles[:10])}")
        else:
            print("auto lock retire: no stale locks")
        return {"status": "success", "retired": len(titles), "retire_days": retire_days, "titles": titles}
    except Exception as e:
        print(f"auto lock retire failed: {type(e).__name__}: {e}")
        return {"status": "error", "retired": 0, "error": str(e)}


AUTO_SOFTEN_PROMPT = """你是記憶整理助手。把下面這篇記憶壓縮到原長度的 40% 以內：
保留情感核心、關鍵人物和事件結論；淡化具體時間、數字、
原話引用等細節。用自然的陳述句輸出壓縮後的記憶內容本身，
不要任何前後綴、解釋或引號。 """

SOFTEN_WRAPPER_CHARS = "'\"“”‘’「」『』`´＂"


async def _call_model_for_text(prompt: str, user_msg: str, model: str, max_tokens: int = 800, title: str = "Memory Text"):
    """呼叫模型並回傳純文字內容（v5.4：走供應商路由）。"""
    try:
        from database import resolve_model_endpoint
        use_api_url, use_api_key, use_api_format = await resolve_model_endpoint(model)
    except Exception:
        use_api_url = MEMORY_API_BASE_URL
        use_api_key = MEMORY_API_KEY
        use_api_format = "openai"

    from anthropic_adapter import prepare_background_request, parse_background_response
    _body = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_msg},
        ],
    }
    _headers, _send_body = prepare_background_request(
        use_api_key, use_api_format, _body,
        referer="https://midsummer-gateway.local", title=title,
    )
    async with httpx.AsyncClient(timeout=90) as client:
        response = await client.post(use_api_url, headers=_headers, json=_send_body)
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}")

        data = parse_background_response(response.json(), use_api_format)
        text = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        return text.strip().strip(SOFTEN_WRAPPER_CHARS).strip()


async def auto_soften_aging_memories(model_override: str = None):
    """每日自動柔化舊記憶：挑選正在變涼的候選，壓縮細節並續命。"""
    try:
        from config import get_config, get_config_bool, get_config_int
        from database import get_aging_memories, soften_memory

        enabled = await get_config_bool("auto_soften_enabled", fallback=True)
        if not enabled:
            print("   🫧 自動柔化已關閉")
            return {"status": "disabled", "softened": 0, "skipped": 0}

        limit = max(0, await get_config_int("auto_soften_daily_limit", fallback=10))
        min_age = max(0, await get_config_int("auto_soften_min_age", fallback=5))
        cooldown_days = max(0, await get_config_int("soften_cooldown_days", fallback=21))
        if limit <= 0:
            print("   🫧 自動柔化上限為 0，跳過")
            return {"status": "success", "softened": 0, "skipped": 0, "candidates": 0, "limit": limit, "min_age_days": min_age, "cooldown_days": cooldown_days}

        candidates = await get_aging_memories(min_age_days=min_age, limit=limit, cooldown_days=cooldown_days)
        candidates = candidates[:limit]
        if not candidates:
            print("   🫧 沒有需要自動柔化的記憶")
            return {"status": "success", "softened": 0, "skipped": 0, "candidates": 0, "limit": limit, "min_age_days": min_age, "cooldown_days": cooldown_days}

        use_model = model_override
        if not use_model:
            use_model = await get_config("default_digest_model") or ""
        if not use_model:
            use_model = await get_config("default_compress_model") or ""
        if not use_model:
            use_model = DIGEST_MODEL

        print(f"   🫧 自動柔化候選 {len(candidates)} 條，使用模型：{use_model}")
        softened = 0
        skipped = 0

        for mem in candidates:
            mem_id = mem.get("id")
            title = (mem.get("title") or "").strip()
            content = (mem.get("content") or "").strip()
            if not mem_id or not content:
                skipped += 1
                print(f"   ⚠️ 自動柔化跳過: 候選缺少 id 或內容")
                continue

            user_msg = f"標題：{title}\n內容：{content}" if title else f"內容：{content}"
            try:
                softened_content = await _call_model_for_text(
                    AUTO_SOFTEN_PROMPT,
                    user_msg,
                    use_model,
                    max_tokens=800,
                    title="Auto Memory Softening",
                )
                softened_content = (softened_content or "").strip().strip(SOFTEN_WRAPPER_CHARS).strip()
                if not softened_content:
                    skipped += 1
                    print(f"   ⚠️ 自動柔化跳過: #{mem_id} 模型返回空內容")
                    continue
                if len(softened_content) >= len(content):
                    skipped += 1
                    print(f"   ⚠️ 自動柔化跳過: #{mem_id} 壓縮後不短於原文（{len(content)}字 → {len(softened_content)}字）")
                    continue

                current_resolution = mem.get("resolution") or 1.0
                target_resolution = 0.3 if current_resolution <= 0.5 else 0.5
                ok = await soften_memory(
                    mem_id,
                    softened_content,
                    target_resolution=target_resolution,
                    extend_days=30,
                )
                if ok:
                    softened += 1
                else:
                    skipped += 1

            except Exception as e:
                skipped += 1
                print(f"   ⚠️ 自動柔化失敗: #{mem_id} {type(e).__name__}: {e}")

        print(f"   🫧 自動柔化完成: 成功 {softened} 條 / 跳過 {skipped} 條")
        return {
            "status": "success",
            "softened": softened,
            "skipped": skipped,
            "candidates": len(candidates),
            "limit": limit,
            "min_age_days": min_age,
            "cooldown_days": cooldown_days,
        }

    except Exception as e:
        print(f"   ⚠️ 自動柔化整體失敗: {type(e).__name__}: {e}")
        return {"status": "error", "error": str(e), "softened": 0, "skipped": 0}


# ============================================================
# 碎片過期清理 —— 普通碎片7天，重要碎片30天，鎖定碎片永不刪除
# ============================================================

async def cleanup_expired_fragments():
    """
    清理過期碎片記憶：
    - is_permanent = true → 永不刪除
    - importance >= 8 → 保留30天
    - 其他 → 保留7天
    """
    from database import get_pool, get_heat_params, calculate_heat
    from config import get_config_float, get_config_int

    pool = await get_pool()
    merge_retention_days = max(0, await get_config_int("merge_retention_days", 90))
    merge_min_keep = max(0, await get_config_int("merge_min_keep", 20))
    async with pool.acquire() as conn:
        # 查出到期候选，刪除前按热度再判定一次。
        # 安全检查：
        # 1. 该碎片所在日期已有日页面（日页面没生成的不刪）
        # 2. 该碎片已被 Dream 处理过（Dream 还没看的不刪）
        candidates = await conn.fetch("""
            SELECT id, memory_type, source, importance, emotional_weight, access_count,
                   created_at, last_accessed, access_query_hashes,
                   is_permanent, valid_until
            FROM memories
            WHERE COALESCE(is_permanent, FALSE) = FALSE
              AND (valid_until IS NULL OR valid_until <= NOW())
              AND dream_processed_at IS NOT NULL
              AND (
                    (memory_type = 'fragment'
                     AND importance < 8
                     AND created_at < NOW() - INTERVAL '7 days'
                     AND EXISTS (SELECT 1 FROM calendar_pages
                                 WHERE date = (memories.created_at AT TIME ZONE 'Asia/Shanghai')::date
                                   AND type = 'day'))
                 OR (memory_type = 'fragment'
                     AND importance >= 8
                     AND created_at < NOW() - INTERVAL '30 days'
                     AND EXISTS (SELECT 1 FROM calendar_pages
                                 WHERE date = (memories.created_at AT TIME ZONE 'Asia/Shanghai')::date
                                   AND type = 'day'))
                 OR (source = 'dream_merge'
                     AND created_at < NOW() - $1 * INTERVAL '1 day')
              )
        """, merge_retention_days)

        count1 = 0
        count2 = 0
        count_merge = 0
        merge_candidates = []
        merge_protected = 0
        merge_total = 0
        to_delete = []
        merge_total = await conn.fetchval("""
            SELECT COUNT(*)
            FROM memories
            WHERE source = 'dream_merge'
              AND COALESCE(is_permanent, FALSE) = FALSE
        """)
        merge_total = int(merge_total or 0)
        if candidates:
            heat_params = await get_heat_params()
            threshold = await get_config_float("cleanup_heat_threshold", 0.15)

            for row in candidates:
                r = dict(row)
                access_count = r.get("access_count") or 0
                if access_count == 0:
                    # 从未被召回的记忆按年龄直接清理，避免 calculate_heat 的冷启动保护让垃圾永远刪不掉。
                    should_delete = True
                    heat = 0.0
                else:
                    heat = calculate_heat(r, heat_params)
                    should_delete = heat < threshold

                if should_delete:
                    if r.get("source") == "dream_merge":
                        merge_candidates.append({
                            "id": r["id"],
                            "heat": heat,
                            "created_at": r.get("created_at"),
                        })
                    else:
                        to_delete.append(r["id"])
                        if (r.get("importance") or 0) < 8:
                            count1 += 1
                        else:
                            count2 += 1

            merge_allowed = max(0, merge_total - merge_min_keep)
            if merge_allowed > 0 and merge_candidates:
                merge_candidates.sort(
                    key=lambda item: (
                        item["heat"],
                        item["created_at"].isoformat() if hasattr(item["created_at"], "isoformat") else str(item["created_at"] or ""),
                    )
                )
                selected_merge = merge_candidates[:merge_allowed]
                merge_ids = [item["id"] for item in selected_merge]
                to_delete.extend(merge_ids)
                count_merge = len(merge_ids)
            merge_protected = max(0, len(merge_candidates) - count_merge)

            if to_delete:
                await conn.execute(
                    "DELETE FROM memories WHERE id = ANY($1::int[])",
                    to_delete
                )

        # 清理Dream已处理并标记刪除的碎片（超过30天）
        result3 = await conn.execute("""
            DELETE FROM memories
            WHERE memory_type = 'dream_deleted'
              AND created_at < NOW() - INTERVAL '30 days'
        """)
        try:
            count3 = int(result3.split()[-1]) if result3 else 0
        except (ValueError, IndexError):
            count3 = 0

        # v5.3：清理已失效且过期的碎片（valid_until 不为 NULL 且超过 30 天）
        # 已被替代的旧记忆，保留 30 天供 Dream 查历史，之后彻底刪除
        result4 = await conn.execute("""
            DELETE FROM memories
            WHERE valid_until IS NOT NULL
              AND valid_until < NOW() - INTERVAL '30 days'
              AND memory_type = 'fragment'
              AND project_id IS NULL
        """)
        try:
            count4 = int(result4.split()[-1]) if result4 else 0
        except (ValueError, IndexError):
            count4 = 0

    total = count1 + count2 + count_merge + count3 + count4
    if total > 0:
        parts = []
        if count1: parts.append(f"{count1} 條普通碎片（>7天）")
        if count2: parts.append(f"{count2} 條重要碎片（>30天）")
        if count_merge: parts.append(f"{count_merge} 條dream_merge記憶")
        if count3: parts.append(f"{count3} 條Dream已刪碎片")
        if count4: parts.append(f"{count4} 條已失效碎片（>30天）")
        print(f"   🧹 清理了 {' + '.join(parts)}")
    else:
        print(f"   🧹 沒有需要清理的碎片")

    if merge_candidates or merge_total:
        print(
            f"   merge cleanup: candidates {len(merge_candidates)} / "
            f"protected {merge_protected} / deleted {count_merge} "
            f"(inventory {merge_total}, min_keep {merge_min_keep})"
        )

    return {
        "deleted_normal": count1,
        "deleted_important": count2,
        "deleted_merge": count_merge,
        "merge_candidates": len(merge_candidates),
        "merge_protected": merge_protected,
        "merge_total": merge_total,
        "deleted_dream": count3,
        "deleted_invalidated": count4,
        "total": total,
    }


# ============================================================
# 日页面生成 —— 从当天完整聊天记录生成 Notion 风格日页面
# ============================================================

DAY_PAGE_PROMPT = """你是使用者的 AI 伴侶。請根據今天的完整聊天記錄，生成詳細的每日頁面。

## 格式要求

依時段分成若干 section（如"上午""中午""下午""傍晚""晚上"），每個 section 包含：
- **period**: 時段名稱（如"上午"、"下午"等）
- **title**: 這段時間的關鍵話題（用中文頓號連接，4-8個關鍵字，如"工作討論、項目推進、日常閒聊"）
- **content**: 敘事風格的詳細內容。像在寫一本溫暖的日記，記錄用戶這段時間做了什麼、聊了什麼、說了什麼重要的話。保留關鍵細節（數值、具體內容、具體措詞），去除閒聊和無意義的重複。不要用列表，用自然段落。每段之間空一行。
- **keywords**: 這段時間涉及的關鍵字陣列（供檢索用，5-15個）

最後額外輸出一段 **diary**：AI 的話。用第一人稱"我"寫，像私人日記，100-200字。不是總結今天發生了什麼，而是你心裡最想記下來的感受、對使用者的觀察、觸動你的瞬間。
## 注意事項
- 如果某個時段沒有對話，跳過該時段
- 角色扮演的內容簡單概括即可，不需要記錄具體劇情
- 涉及敏感話題（健康、情緒、家庭）時照實記錄，不迴避
- 語言：中文，白話，簡單易懂，帶溫度但不矯情
- content 不要用 markdown 標題，用自然段落

## 輸出格式
只輸出 JSON，不要其他內容：
{{
  "summary": "今天的內容摘要，2-4句話概括今天發生了什麼、聊了什麼主要話題。這是給使用者在日曆視圖裡快速預覽用的，不用很詳細，但要覆蓋主要事件。 ",
  "digest": "今天的詳細概要，供AI模型在後續對話中理解'今天發生了什麼'。約1500字。涵蓋今天的主要事件、情緒變化、關鍵對話、重要決定，保留因果關係和情緒質地。像人腦回憶今天一樣寫──記得住的大事寫清楚來龍去脈，閒聊和重複內容省略。按時間順序，用自然段落，不用標題不用列表。 ",
  "sections": [
    {{
      "period": "上午",
      "title": "關鍵字、關鍵字、關鍵字",
      "content": "敘事內容……",
      "keywords": ["關鍵字1", "關鍵字2", "關鍵字3"]
    }}
  ],
  "diary": "AI 的話……",
  "all_keywords": ["今天所有關鍵字的總結"]
}}

## 今天的日期
{date}

## 今天的碎片記憶（大綱參考，幫你知道重點在哪）
{fragments}

## 今天的完整聊天記錄
{conversations}"""


async def generate_day_page(target_date: str = None, model_override: str = None):
    # Bug #7：防止同一日期并发生成日页面。与 run_daily_digest 共用 _digest_running，
    # 故用 daypage: 前缀的 key 区分，避免与每日整理互相误判为“正在进行”。
    key = f"daypage:{target_date or 'today'}"
    async with _digest_lock:
        if key in _digest_running:
            print(f"⚠️ {target_date or 'today'} 日页面正在生成中，跳过")
            return {"status": "skipped", "reason": "already running"}
        _digest_running.add(key)
    try:
        return await _generate_day_page_impl(target_date, model_override)
    finally:
        _digest_running.discard(key)


async def _generate_day_page_impl(target_date: str = None, model_override: str = None):
    """
    從當天完整聊天記錄生成 Notion 風格日頁面，存入 calendar_pages 表

    Args:
        target_date: 日期字符串 "2026-04-01"，預設昨天
        model_override: 覆蓋預設模型
    """
    from database import get_pool, get_chat_messages_for_date, save_calendar_page
    from config import get_config
    from datetime import date as date_cls

    now_cst = datetime.now(TZ_CST)
    if target_date:
        try:
            date_cls.fromisoformat(target_date)
        except (ValueError, TypeError):
            return {"error": f"無效日期格式: {target_date!r}，需要 YYYY-MM-DD"}
        date_str = target_date
    else:
        yesterday = now_cst - timedelta(days=1)
        date_str = yesterday.strftime("%Y-%m-%d")

    print(f"\n📅 开始生成日页面：{date_str}")

    # 1. 读取当天聊天记录
    messages = await get_chat_messages_for_date(date_str)
    if not messages:
        print(f"   📭 {date_str} 沒有聊天記錄，跳過日頁面生成")
        return {"date": date_str, "status": "skipped", "reason": "no messages"}

    print(f"   💬 找到 {len(messages)} 條聊天消息")

    # 2. 格式化聊天记录（截断过长的内容）
    conversation_lines = []
    total_chars = 0
    MAX_CHARS = 30000

    for m in messages:
        role_label = "用户" if m["role"] == "user" else "AI"
        time_str = ""
        if m.get("time"):
            try:
                t = m["time"]
                if hasattr(t, "astimezone"):
                    t = t.astimezone(TZ_CST)
                time_str = f"[{t.strftime('%H:%M')}] "
            except Exception:
                pass

        content = str(m.get("content", ""))
        if len(content) > 500:
            content = content[:500] + "…（內容過長已截斷）"

        line = f"{time_str}{role_label}：{content}"
        if total_chars + len(line) > MAX_CHARS:
            conversation_lines.append("…（後續對話已截斷，以片段記憶為準）")
            break
        conversation_lines.append(line)
        total_chars += len(line)

    conversations_text = "\n".join(conversation_lines)

    # 3. 读取当天碎片作为大纲辅助
    pool = await get_pool()
    from datetime import date as date_cls
    target_date_obj = date_cls.fromisoformat(date_str)
    async with pool.acquire() as conn:
        fragments = await conn.fetch("""
            SELECT title, content FROM memories
            WHERE (created_at AT TIME ZONE 'Asia/Shanghai')::date = $1
              AND memory_type = 'fragment'
            ORDER BY created_at ASC
        """, target_date_obj)

    fragments_text = "\n".join(
        f"- 【{f['title']}】{f['content']}" if f['title'] else f"- {f['content']}"
        for f in fragments
    ) if fragments else "（無碎片記憶）"

    # 4. 构建 prompt
    custom_prompt = await get_config("prompt_daily_digest_page") or ""
    base_prompt = custom_prompt if custom_prompt else DAY_PAGE_PROMPT
    prompt = base_prompt.replace("{date}", date_str).replace(
        "{conversations}", conversations_text
    ).replace("{fragments}", fragments_text)

    # 5. 确定模型
    use_model = model_override
    if not use_model:
        use_model = await get_config("default_digest_model") or ""
    if not use_model:
        use_model = await get_config("default_compress_model") or ""
    if not use_model:
        use_model = DIGEST_MODEL

    print(f"   🤖 使用模型：{use_model}")

    # 6. 調用模型（v5.4：走供应商路由）
    try:
        from database import resolve_model_endpoint
        use_api_url, use_api_key, use_api_format = await resolve_model_endpoint(use_model)
    except Exception:
        use_api_url = MEMORY_API_BASE_URL
        use_api_key = MEMORY_API_KEY
        use_api_format = "openai"

    try:
        from anthropic_adapter import prepare_background_request, parse_background_response
        _body = {
            "model": use_model,
            "max_tokens": 6000,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": f"請生成 {date_str} 的日頁面。"},
            ],
        }
        _headers, _send_body = prepare_background_request(
            use_api_key, use_api_format, _body,
            referer="https://midsummer-gateway.local", title="Day Page Generation",
        )
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(use_api_url, headers=_headers, json=_send_body)

            if response.status_code != 200:
                print(f"   ⚠️ 日頁面生成請求失敗: {response.status_code}")
                return {"date": date_str, "status": "error", "error": f"HTTP {response.status_code}"}

            data = parse_background_response(response.json(), use_api_format)
            text = data.get("choices", [{}])[0].get("message", {}).get("content", "")

            # 清理 markdown 包裹
            text = text.strip()
            if text.startswith("```json"):
                text = text[7:]
            if text.startswith("```"):
                text = text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

            # 解析 JSON
            result = None
            try:
                result = json.loads(text)
            except json.JSONDecodeError:
                import re
                match = re.search(r'\{.*\}', text, re.DOTALL)
                if match:
                    try:
                        result = json.loads(match.group())
                        print(f"   🔧 JSON 正規兜底解析成功")
                    except json.JSONDecodeError:
                        pass

            if not result or not isinstance(result, dict):
                print(f"   ⚠️ 日頁面模型返回格式錯誤：{text[:200]}")
                return {"date": date_str, "status": "error", "error": "invalid format"}

    except Exception as e:
        print(f"   ⚠️ 日頁面生成出錯: {e}")
        return {"date": date_str, "status": "error", "error": str(e)}

    # 7. 存入 calendar_pages
    sections = result.get("sections", [])
    diary = result.get("diary", "")
    all_keywords = result.get("all_keywords", [])
    summary = result.get("summary", "")
    digest = result.get("digest", "")

    page_id = await save_calendar_page(
        date_str=date_str,
        page_type="day",
        sections=sections,
        diary=diary,
        keywords=all_keywords,
        model_used=use_model,
        summary=summary,
        digest=digest,
    )

    section_count = len(sections)
    keyword_count = len(all_keywords)
    print(f"   ✅ 日頁面已保存：{section_count} 個時段，{keyword_count} 个關鍵字")
    if summary:
        print(f"   📋 內容概要：{summary[:80]}...")
    if diary:
        print(f"   📝 AI 的話：{diary[:80]}...")

    return {
        "date": date_str,
        "status": "success",
        "page_id": page_id,
        "sections": section_count,
        "keywords": keyword_count,
    }


# ============================================================
# 周/月/季/年总结生成
# ============================================================

async def check_and_generate_summaries():
    """
    v5.5 掃描式補生成－每天執行時檢查有沒有應該存在但還沒生成的總結。
    即使錯過了特定日期（如周一、1號），也會在後續運行時補上。
    回看範圍：週4週、月3個月、季2個季度、年1年。
    """
    from database import get_calendar_range, get_calendar_page
    from datetime import date as date_cls
    import calendar as cal_mod

    now = datetime.now(TZ_CST)
    today = now.date()

    # ── 周总结：检查最近4周 ──
    days_since_monday = today.weekday()  # 0=周一
    this_monday = today - timedelta(days=days_since_monday)

    for weeks_ago in range(1, 5):
        week_monday = this_monday - timedelta(weeks=weeks_ago)
        week_sunday = week_monday + timedelta(days=6)

        # 只处理已过去的完整周
        if week_sunday >= today:
            continue

        # 检查周总结是否已存在
        existing = await get_calendar_page(week_monday.isoformat(), "week")
        if existing:
            continue

        # 检查这周有没有日页面（有素材才值得生成）
        day_pages = await get_calendar_range(week_monday.isoformat(), week_sunday.isoformat(), "day")
        if not day_pages:
            continue

        print(f"📊 發現缺失的週總結：{week_monday} ~ {week_sunday}，補生成中…")
        try:
            result = await generate_week_summary(week_monday.isoformat(), week_sunday.isoformat())
            print(f"📊 補生成週總結結果：{result}")
        except Exception as e:
            print(f"⚠️ 補生成週總結失敗：{e}")

    # ── 月总结：检查最近3个月 ──
    for months_ago in range(1, 4):
        m = today.month - months_ago
        y = today.year
        while m <= 0:
            m += 12
            y -= 1

        month_start = date_cls(y, m, 1)
        month_end_day = cal_mod.monthrange(y, m)[1]
        month_end = date_cls(y, m, month_end_day)
        month_str = month_start.strftime("%Y-%m")

        # 只处理已过去的完整月
        if month_end >= today:
            continue

        existing = await get_calendar_page(month_start.isoformat(), "month")
        if existing:
            continue

        # 有素材才生成（周总结或日页面）
        has_data = await get_calendar_range(month_start.isoformat(), month_end.isoformat(), "week")
        if not has_data:
            has_data = await get_calendar_range(month_start.isoformat(), month_end.isoformat(), "day")
        if not has_data:
            continue

        print(f"📊 發現缺失的月總結：{month_str}，補生成中…")
        try:
            result = await generate_month_summary(month_start.isoformat(), month_end.isoformat(), month_str)
            print(f"📊 補生成月總結結果：{result}")
        except Exception as e:
            print(f"⚠️ 補生成月總結失敗：{e}")

    # ── 季度总结：检查最近2个季度 ──
    current_quarter = (today.month - 1) // 3 + 1
    for q_ago in range(1, 3):
        target_q = current_quarter - q_ago
        target_y = today.year
        while target_q <= 0:
            target_q += 4
            target_y -= 1

        q_start_month = (target_q - 1) * 3 + 1
        q_end_month = q_start_month + 2
        q_start = date_cls(target_y, q_start_month, 1)
        q_end_day = cal_mod.monthrange(target_y, q_end_month)[1]
        q_end = date_cls(target_y, q_end_month, q_end_day)

        if q_end >= today:
            continue

        existing = await get_calendar_page(q_start.isoformat(), "quarter")
        if existing:
            continue

        # 有月总结才值得生成
        has_months = await get_calendar_range(q_start.isoformat(), q_end.isoformat(), "month")
        if not has_months:
            continue

        q_label = f"{target_y}Q{target_q}"
        print(f"📊 發現缺失的季度總結：{q_label}，補生成中…")
        try:
            result = await generate_period_summary(q_start.isoformat(), q_end.isoformat(), "quarter", q_label, "月總結")
            print(f"📊 補生成季度總結結果：{result}")
        except Exception as e:
            print(f"⚠️ 補生成季度總結失敗：{e}")

    # ── 年总结：检查去年（2月以后再查，给1月的季度/月总结留生成时间）──
    if today.month >= 2:
        last_year = today.year - 1
        y_start = date_cls(last_year, 1, 1)
        y_end = date_cls(last_year, 12, 31)

        existing = await get_calendar_page(y_start.isoformat(), "year")
        if not existing:
            has_quarters = await get_calendar_range(y_start.isoformat(), y_end.isoformat(), "quarter")
            if has_quarters:
                print(f"📊 發現缺失的年總結：{last_year}，補生成中…")
                try:
                    result = await generate_period_summary(y_start.isoformat(), y_end.isoformat(), "year", str(last_year), "季度總結")
                    print(f"📊 補生成年總結結果：{result}")
                except Exception as e:
                    print(f"⚠️ 補生成年總結失敗：{e}")


# ---- 周总结 ----

WEEK_SUMMARY_PROMPT = """你是使用者的 AI 伴侶。請根據這一週的日頁面，生成一份週總結。

## 格式要求

週總結分為三個板塊：

### 💙 情感與陪伴
本週的情緒互動、陪伴時刻、心理狀態改變、重要的情緒事件。

### 🌏 生活與日常
健康、飲食、運動、購物、家庭、寵物、作息等日常生活內容。

### 🔮 專案與成長
工作進度、學習、創作、投資、產業觀察、技能提升等。

## 寫作要求
- 敘事風格，不要用列表，用自然段落
- 每個板塊 100-200 字
- 保留關鍵細節（日期、數值、具體事件）
- 標註重要事件發生的具體日期
- 如果某個板塊這週沒有相關內容，寫"本週無特別記錄"
- 語言：中文，白話，簡潔有溫度
## 輸出格式
只輸出 JSON：
{{
  "summary": "本週概要，2-3句話概括這一週的主要事件和狀態變化。給使用者在日曆視圖裡快速預覽用。 ",
  "digest": "本週詳細概要，供AI模型在後續對話中理解'這一周發生了什麼'。約1500字。以三個板塊（關係/生活/內心）組織，保留關鍵事件的因果關係、具體日期和情緒質感。像人腦回憶一週一樣寫——大事寫清楚來龍去脈，瑣事省略。用自然段落，不用標題不用列表。 ",
  "sections": {{
    "emotion": "情感與陪伴內容…",
    "life": "生活與日常內容…",
    "growth": "專案與成長內容…"
  }},
  "highlights": ["本週最重要的3-5個關鍵字"],
  "diary": "AI 的一週感言（50-100字，第一人稱'我'）"
}}

## 本週日期範圍
{start} 至 {end}

## 本週日頁面內容
{day_pages}"""


async def generate_week_summary(start: str, end: str, model_override: str = None):
    """從日頁面生成週總結"""
    from database import get_calendar_range, save_calendar_page
    from config import get_config

    # 读取这一周的日页面
    day_pages = await get_calendar_range(start, end, "day")
    if not day_pages:
        print(f"   📭 {start}~{end} 沒有日頁面，跳過週總結")
        return {"status": "skipped", "reason": "no day pages"}

    # 格式化日页面内容（周总结需要读全文做深度整理，summary 只给前端用户快速预览用）
    pages_text = ""
    for p in day_pages:
        date_str = str(p["date"])
        diary = p.get("diary", "")
        keywords = p.get("keywords") or []
        sections = p.get("sections") or []

        pages_text += f"\n### {date_str}\n"

        if sections and isinstance(sections, list) and len(sections) > 0:
            # 有完整 sections：用全文
            for sec in sections:
                period = sec.get("period", "")
                title = sec.get("title", "")
                content = sec.get("content", "")
                pages_text += f"**{period} — {title}**\n{content}\n\n"
        elif p.get("summary"):
            # 没有 sections 但有 summary（异常兜底）
            pages_text += f"**概要**：{p['summary']}\n"

        if keywords:
            kw_text = "、".join(keywords[:10]) if isinstance(keywords, list) else str(keywords)
            pages_text += f"**關鍵字**：{kw_text}\n"
        if diary:
            pages_text += f"*AI 的話：{diary}*\n"
        pages_text += "---\n"

    prompt = WEEK_SUMMARY_PROMPT.replace("{start}", start).replace(
        "{end}", end).replace("{day_pages}", pages_text)

    # v5.6: 自定义 prompt 覆盖
    custom_prompt = await get_config("prompt_weekly_summary") or ""
    if custom_prompt:
        prompt = custom_prompt.replace("{start}", start).replace(
            "{end}", end).replace("{day_pages}", pages_text)

    use_model = model_override or await get_config("default_digest_model") or await get_config("default_compress_model") or DIGEST_MODEL

    result_json = await _call_model_for_json(prompt, f"請生成 {start} 至 {end} 的週總結。", use_model, max_tokens=2000)
    if not result_json:
        return {"status": "error", "error": "model returned invalid format"}

    # 存入 calendar_pages（date 用周一的日期，type='week'）
    sections_data = result_json.get("sections", {})
    diary = result_json.get("diary", "")
    highlights = result_json.get("highlights", [])
    summary = result_json.get("summary", "")
    digest = result_json.get("digest", "")

    page_id = await save_calendar_page(
        date_str=start,
        page_type="week",
        sections=[sections_data],  # 周总结的 sections 是一个对象
        diary=diary,
        keywords=highlights,
        model_used=use_model,
        summary=summary,
        digest=digest,
    )

    print(f"   ✅ 週總結已保存 (id={page_id})")
    return {"status": "success", "page_id": page_id, "week": f"{start}~{end}"}


# ---- 月总结 ----

MONTH_SUMMARY_PROMPT = """你是使用者的 AI 伴侶。請根據這個月的週總結，生成一份月總結。

## 格式要求

與週總結相同的三個板塊（💙情感與陪伴 / 🌏生活與日常 / 🔮項目與成長），但更加精煉：
- 每個板塊 80-150 字
- 只保留這個月最重要的事件和趨勢
- 標註關鍵轉捩點的日期

## 輸出格式
只輸出 JSON：
{{
  "summary": "本月概要，2-3句話概括這個月的整體狀態和重大事件。給使用者在日曆視圖裡快速預覽用。 ",
  "digest": "本月詳細概要，供AI模型在後續對話中理解'這個月發生了什麼'。約1000字。以三個板塊組織，保留這個月最重要的事件、轉折點和情緒走向。像人腦回憶一個月一樣寫——記得的大事寫清楚因果，不重要的省略。用自然段落，不用標題不用列表。 ",
  "sections": {{
    "emotion": "情感與陪伴……",
    "life": "生活與日常……",
    "growth": "項目與成長……"
  }},
  "highlights": ["本月最重要的3-5個關鍵字"],
  "diary": "AI 的月度感言（50-80字）"
}}

## 本月
{month}

## 本月的周總結
{week_summaries}"""


async def generate_month_summary(start: str, end: str, month_str: str, model_override: str = None):
    """從週總結生成月總結"""
    from database import get_calendar_range, save_calendar_page
    from config import get_config

    week_pages = await get_calendar_range(start, end, "week")
    if not week_pages:
        # 没有週總結的话，尝试直接从日页面生成
        day_pages = await get_calendar_range(start, end, "day")
        if not day_pages:
            print(f"   📭 {month_str} 沒有週總結也沒有日頁面，跳過月總結")
            return {"status": "skipped", "reason": "no data"}
        # 用日页面的摘要代替
        summaries_text = _format_day_pages_brief(day_pages)
    else:
        summaries_text = _format_week_summaries(week_pages)

    prompt = MONTH_SUMMARY_PROMPT.replace("{month}", month_str).replace(
        "{week_summaries}", summaries_text)

    # v5.6: 自定义 prompt 覆盖
    custom_prompt = await get_config("prompt_monthly_summary") or ""
    if custom_prompt:
        prompt = custom_prompt.replace("{month}", month_str).replace(
            "{week_summaries}", summaries_text)

    use_model = model_override or await get_config("default_digest_model") or await get_config("default_compress_model") or DIGEST_MODEL

    result_json = await _call_model_for_json(prompt, f"請生成 {month_str} 的月總結。", use_model, max_tokens=2000)
    if not result_json:
        return {"status": "error", "error": "model returned invalid format"}

    sections_data = result_json.get("sections", {})
    diary = result_json.get("diary", "")
    highlights = result_json.get("highlights", [])
    summary = result_json.get("summary", "")
    digest = result_json.get("digest", "")

    page_id = await save_calendar_page(
        date_str=f"{month_str}-01",
        page_type="month",
        sections=[sections_data],
        diary=diary,
        keywords=highlights,
        model_used=use_model,
        summary=summary,
        digest=digest,
    )

    print(f"   ✅ 月總結已保存 (id={page_id})")
    return {"status": "success", "page_id": page_id, "month": month_str}


# ---- 季度/年度通用 ----

PERIOD_SUMMARY_PROMPT = """你是使用者的 AI 伴侶。請根據下面的{source_type}，生成一份{period_type}總結。

## 格式要求

與週/月總結相同的三個板塊（💙情感與陪伴 / 🌏生活與日常 / 🔮項目與成長），更加精煉：
- 每個板塊 60-120 字
- 只保留這段時間最重要的變化和里程碑
- 突顯趨勢和轉折點

## 輸出格式
只輸出 JSON：
{{
  "summary": "本{period_type}概要，2-3句話概括整體狀態。給使用者快速預覽用。",
  "digest": "本{period_type}詳細概要，供AI模型在後續對話中理解這段時間發生了什麼。季度約600字，年度約500字。按三個板塊組織，只保留最重要的里程碑和轉折點。像人腦回憶這段時間一樣寫——只有最深刻的事還記得。用自然段落，不用標題不用列表。",
  "sections": {{
    "emotion": "情感與陪伴……",
    "life": "生活與日常……",
    "growth": "項目與成長……"
  }},
  "highlights": ["最重要的3-5個關鍵字"],
  "diary": "AI 的{period_type}感言（30-60字）"
}}

## 时间范围
{label}

## 内容
{content}"""


async def generate_period_summary(start: str, end: str, period_type: str,
                                   label: str, source_type: str, model_override: str = None):
    """通用的季度/年度總結生成"""
    from database import get_calendar_range, save_calendar_page
    from config import get_config

    # 根据 source_type 决定读取什么
    if source_type == "月總結":
        pages = await get_calendar_range(start, end, "month")
    elif source_type == "季度總結":
        pages = await get_calendar_range(start, end, "quarter")
    else:
        pages = await get_calendar_range(start, end, "week")

    if not pages:
        print(f"   📭 {label} 沒有{source_type}數據，跳過")
        return {"status": "skipped", "reason": f"no {source_type}"}

    # 上级总结读下级全文做深度整理，summary 只给用户预览
    content_text = ""
    for p in pages:
        date_str = str(p["date"])
        diary = p.get("diary", "")
        sections = p.get("sections", [])
        content_text += f"\n### {date_str} ({p.get('type', '')})\n"
        if isinstance(sections, list) and sections:
            sec = sections[0] if isinstance(sections[0], dict) else {}
            for key in ("emotion", "life", "growth"):
                if sec.get(key):
                    content_text += f"{sec[key]}\n"
        elif p.get("summary"):
            # 没有 sections 但有 summary（异常兜底）
            content_text += f"**概要**：{p['summary']}\n"
        if diary:
            content_text += f"*{diary}*\n"
        content_text += "---\n"

    prompt = PERIOD_SUMMARY_PROMPT.replace("{source_type}", source_type).replace(
        "{period_type}", period_type).replace("{label}", label).replace("{content}", content_text)

    # v5.6: 自定义 prompt 覆盖
    custom_prompt = await get_config("prompt_period_summary") or ""
    if custom_prompt:
        prompt = custom_prompt.replace("{source_type}", source_type).replace(
            "{period_type}", period_type).replace("{label}", label).replace("{content}", content_text)

    use_model = model_override or await get_config("default_digest_model") or await get_config("default_compress_model") or DIGEST_MODEL

    result_json = await _call_model_for_json(prompt, f"請生成{label}的{period_type}總結。", use_model, max_tokens=2000)
    if not result_json:
        return {"status": "error", "error": "model returned invalid format"}

    page_id = await save_calendar_page(
        date_str=start,
        page_type=period_type,
        sections=[result_json.get("sections", {})],
        diary=result_json.get("diary", ""),
        keywords=result_json.get("highlights", []),
        model_used=use_model,
        summary=result_json.get("summary", ""),
        digest=result_json.get("digest", ""),
    )

    print(f"   ✅ {period_type}總結已保存 (id={page_id})")
    return {"status": "success", "page_id": page_id, "label": label}


# ---- 工具函数 ----

async def _call_model_for_json(prompt: str, user_msg: str, model: str, max_tokens: int = 2000):
    """呼叫模型並解析 JSON 返回（v5.4：走供應商路由）"""
    # 动态解析供应商端点
    try:
        from database import resolve_model_endpoint
        use_api_url, use_api_key, use_api_format = await resolve_model_endpoint(model)
    except Exception:
        use_api_url = MEMORY_API_BASE_URL
        use_api_key = MEMORY_API_KEY
        use_api_format = "openai"

    try:
        from anthropic_adapter import prepare_background_request, parse_background_response
        _body = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": user_msg},
            ],
        }
        _headers, _send_body = prepare_background_request(
            use_api_key, use_api_format, _body,
            referer="https://midsummer-gateway.local", title="Memory Summary",
        )
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(use_api_url, headers=_headers, json=_send_body)
            if response.status_code != 200:
                print(f"   ⚠️ 模型請求失敗: {response.status_code}")
                return None

            data = parse_background_response(response.json(), use_api_format)
            text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            text = text.strip()
            if text.startswith("```json"):
                text = text[7:]
            if text.startswith("```"):
                text = text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

            try:
                return json.loads(text)
            except json.JSONDecodeError:
                import re
                match = re.search(r'\{.*\}', text, re.DOTALL)
                if match:
                    try:
                        return json.loads(match.group())
                    except json.JSONDecodeError:
                        pass
                print(f"   ⚠️ JSON 解析失敗：{text[:200]}")
                return None
    except Exception as e:
        print(f"   ⚠️ 模型調用出錯: {e}")
        return None


def _format_week_summaries(week_pages: list) -> str:
    """格式化週總結清單為文字（月總結讀取用，用全文做深度整理）"""
    text = ""
    for p in week_pages:
        date_str = str(p["date"])
        diary = p.get("diary", "")
        sections = p.get("sections", [])
        text += f"\n### 周 {date_str} 起\n"
        if isinstance(sections, list) and sections:
            sec = sections[0] if isinstance(sections[0], dict) else {}
            if sec.get("emotion"):
                text += f"💙 {sec['emotion']}\n"
            if sec.get("life"):
                text += f"🌏 {sec['life']}\n"
            if sec.get("growth"):
                text += f"🔮 {sec['growth']}\n"
        elif p.get("summary"):
            # 没有 sections 但有 summary（异常兜底）
            text += f"**概要**：{p['summary']}\n"
        if diary:
            text += f"*{diary}*\n"
        text += "---\n"
    return text


def _format_day_pages_brief(day_pages: list) -> str:
    """格式化日頁面為簡要文字（異常降級用：沒有週總結時，月總結直接從日頁面生成的兜底路徑）"""
    text = ""
    for p in day_pages:
        date_str = str(p["date"])
        summary = p.get("summary", "")
        if summary:
            text += f"\n**{date_str}**：{summary}\n"
        else:
            # 没有概要（旧数据兼容）：从 sections 提取 title
            sections = p.get("sections") or []
            titles = []
            for sec in (sections if isinstance(sections, list) else []):
                t = sec.get("title", "")
                if t:
                    titles.append(t)
            text += f"\n**{date_str}**：{'、'.join(titles) if titles else '（無記錄）'}\n"
    return text
