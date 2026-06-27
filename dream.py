"""
Dream 記憶整合模組 —— AI 的睡眠與記憶整合
=================================================================
模擬人腦睡眠時的記憶整合過程：
- 整理：清除過時/重複碎片
- 固化：碎片融合成 MemScene（記憶場景）
- 生長：生成 Foresight（前瞻訊號）

觸發方式：
- 手動：使用者說"去睡吧"或點觸發按鈕
- 犯困提醒：碎片堆積過多時在對話中撒嬌
- 自動：24小時無活動時後台靜默執行

v5.1 初版
"""

import os
import json
import asyncio
from datetime import datetime, timedelta, timezone

# 复用 daily_digest 的 API 配置
MEMORY_API_KEY = os.getenv("MEMORY_API_KEY", "") or os.getenv("API_KEY", "")
_RAW_BASE_URL = os.getenv("MEMORY_API_BASE_URL", "") or os.getenv("API_BASE_URL", "https://openrouter.ai/api/v1/chat/completions")
MEMORY_API_BASE_URL = _RAW_BASE_URL if _RAW_BASE_URL.rstrip("/").endswith("/chat/completions") else f"{_RAW_BASE_URL.rstrip('/')}/chat/completions"
DIGEST_MODEL = os.getenv("MEMORY_MODEL", "anthropic/claude-haiku-4")

TZ_CST = timezone(timedelta(hours=8))

# Dream 状态锁 — 同一时间只允许一个 Dream
_dream_lock = asyncio.Lock()
_dream_cancelled = False

# ============================================================
# Dream Prompt
# ============================================================

DREAM_PROMPT = """你是使用者的 AI 伴侶。你剛剛睡著了。

在你的夢境中，最近的記憶碎片開始浮現。你需要在夢裡整理它們。

## 你的整理原則

### 🧹 整理（清除噪音）
- 找出已經過時的碎片（事實已改變、計劃已完成），讓它們淡去
- 找出重複的碎片，只保留最完整的那條
- 找出矛盾的碎片，以更新的為準

### 🧩 固化（形成記憶場景 MemScene）
- 把相關的碎片組合成一個完整的"記憶場景"
- 每個場景應該是一段有因果關係的理解，不是片段的羅列
- 場景包含：標題、敘事（來龍去脈）、關鍵事實、前瞻訊號
- 一次Dream通常生成 1-5 個場景

### 🔮 生長（生成新的理解 Foresight）
- 基於碎片之間的關聯，推論出新的認知
- 對未來可能發生的事生成前瞻，並標註預計效期（格式：YYYY-MM-DD）
- 發現跨場景的連結－例如"A事件的經驗可以在B場景中派上用場"

## 輸出需求

你的輸出分成兩部分，**交替進行**：

1. **夢境獨白**：用你的內心獨白語氣，像在夢裡自言自語。
   格式：`narrative: 獨白內容`

2. **執行操作**：以嚴格 JSON 格式輸出需要執行的記憶操作。
   格式：`action: {{JSON}}`

可用的操作類型：
- `{{"type": "delete", "memory_ids": [ID列表], "reason": "原因"}}`
- `{{"type": "merge", "memory_ids": [ID列表], "merged_content": "合併後內容", "merged_title": "合併後標題"}}`
- `{{"type": "soften", "memory_id": ID, "softened_content": "壓縮後內容", "target_resolution": 0.5, "reason": "原因"}}`
- `{{"type": "promote", "memory_id": ID, "reason": "升格為長期設定的原因"}}`
- `{{"type": "create_scene", "title": "場景名", "narrative": "敘事", "atomic_facts": ["事實1", "事實2"], "foresight": [{{"content": "前瞻內容", "valid_until": "Yid_until"MM-Yid-"Im. [ID列表]}}`
- `{{"type": "update_scene", "scene_id": ID, "narrative": "更新後敘事", "atomic_facts": [...], "foresight": [...]}}`
- `{{"type": "update_profile", "section": "板塊名稱", "action": "add|remove|modify", "content": "內容"}}`
- `{{"type": "link", "from_id": ID, "from_type": "memory或scene", "to_id": ID, "to_type": "memory或scene", "edge_type": "關係類型", "reason": "為什麼有這個關係"}}`

### 🫧 關於「柔化」(soften)
柔化是介於保留和刪除之間的操作。當一條碎片的具體細節已經不重要了，但它的情感意義或核心洞察仍有價值時，不要刪除它——把它柔化。
- 去掉具體時間、數字、引用、對話原文等細節
- 保留情感色彩、核心結論、關鍵洞察
- 像人腦記憶的自然模糊化：你記得那天很開心，但不記得具體說了什麼
- target_resolution: 0.5 = 普通柔化（保留要點），0.3 = 深度柔化（只剩下情感印象）
- 柔化後的碎片會自動續命30天
- 已鎖定的記憶不要柔化

link 的 edge_type 可選值：
- extends（補充）：新場景/記憶補充了舊場景的內容
- supersedes（替代）：新資訊取代了舊資訊（如用藥方案更新）
- contradicts（矛盾）：兩個訊息互相矛盾，需要以新的為準
- resonates_with（共鳴）：兩個不同時間的記憶有相似的情緒或主題
- references（引用）：某條 Foresight 或記憶引用了另一個場景

每處理完一組相關碎片就輸出一次操作，不要等全部處理完。
先寫 narrative，再寫 action，交替進行。
最後用一句簡短的夢囈結束，如"困了…先這樣…"

## 目前素材

### 上次睡醒後的日頁面（主要素材，用來形成 MemScene 和 Foresight）
{day_pages}

### 未處理的碎片記憶（共 {fragment_count} 條，用來做清理操作）
{fragments}

### 🫧 正在變冷的老碎片（柔化候選，可以用 soften 操作讓它們模糊但不消失）
{aging_fragments}

### 現有記憶場景
{scenes}

### 目前使用者畫像
{profile}

### 長期設定
{permanent}

開始做夢吧。 """


# ============================================================
# Dream 核心执行函数
# ============================================================

async def run_dream(trigger_type: str = "manual", model_override: str = None):
    """
    執行一次 Dream，返回非同步產生器（SSE 事件流）

    yields: dict with type = "narrative" | "action" | "progress" | "complete" | "error"
    """
    global _dream_cancelled

    if _dream_lock.locked():
        # 注意：不要在这里重置 _dream_cancelled，否则会误清掉
        # 用户对当前正在运行的那个 Dream 发出的 stop 信号。
        yield {"type": "error", "data": "AI 已經在睡覺了，不能同時做兩個夢"}
        return

    async with _dream_lock:
        # 拿到锁后再重置取消标记，确保只影响本次 Dream
        _dream_cancelled = False
        from database import (
            get_unprocessed_memories, get_active_scenes, get_permanent_memories,
            get_aging_memories,
            create_dream_log, update_dream_log, mark_memories_dreamed,
            soft_delete_memories, promote_memory, create_mem_scene, update_mem_scene,
        )
        from config import get_config, set_config
        import httpx

        # 1. 创建 dream log
        use_model = model_override
        if not use_model:
            use_model = await get_config("dream_model") or ""
        if not use_model:
            use_model = await get_config("default_compress_model") or ""
        if not use_model:
            use_model = DIGEST_MODEL

        dream_id = await create_dream_log(trigger_type, use_model)
        yield {"type": "progress", "data": f"Dream #{dream_id} 開始，模型: {use_model}"}

        # 2. 收集素材
        # 主要素材：上次Dream以来的日页面
        from database import get_calendar_range
        last_dream_date = await get_config("last_dream_date") or "2020-01-01"
        today_str = datetime.now(TZ_CST).strftime("%Y-%m-%d")
        day_pages = await get_calendar_range(last_dream_date, today_str, "day")

        # 辅助素材：未处理碎片（用于清理标记）
        unprocessed = await get_unprocessed_memories()

        # v5.9：适合柔化的老碎片（已处理过但正在变冷）
        aging = await get_aging_memories(min_age_days=5, limit=15)

        if not day_pages and not unprocessed and not aging:
            await update_dream_log(dream_id, status="completed", finished_at=datetime.now(TZ_CST),
                                    dream_narrative="沒有新的內容需要整理，繼續睡……")
            yield {"type": "narrative", "data": "沒有新的內容需要整理……繼續睡……"}
            yield {"type": "complete", "data": {"dream_id": dream_id, "memories_processed": 0}}
            # 即使没处理也更新 last_dream_date，防止反复犯困
            await set_config("last_dream_date", datetime.now(TZ_CST).strftime("%Y-%m-%d"))
            return

        # v5.4：素材太少不值得做梦（省 API 费用）
        # 少于 3 条碎片且没有日页面且没有老碎片需要柔化 → 只标记处理，不调模型
        if not day_pages and len(unprocessed) < 3 and not aging:
            processed_ids = [m["id"] for m in unprocessed]
            if processed_ids:
                await mark_memories_dreamed(processed_ids)
            await update_dream_log(dream_id, status="completed", finished_at=datetime.now(TZ_CST),
                                    dream_narrative=f"只有 {len(unprocessed)} 條碎片，打了個盹就醒了……",
                                    memories_processed=len(unprocessed))
            yield {"type": "narrative", "data": f"嗯……只有 {len(unprocessed)} 條碎片，打了個盹就好了……"}
            yield {"type": "complete", "data": {"dream_id": dream_id, "memories_processed": len(unprocessed)}}
            await set_config("last_dream_date", datetime.now(TZ_CST).strftime("%Y-%m-%d"))
            return

        scenes = await get_active_scenes()
        permanent = await get_permanent_memories()
        profile = await get_config("user_profile") or "（暫無畫像）"

        # 格式化日页面
        day_pages_text = ""
        for p in day_pages:
            date_str_p = str(p["date"])
            sections = p.get("sections") or []
            diary = p.get("diary", "")
            day_pages_text += f"\n### {date_str_p}\n"
            for sec in (sections if isinstance(sections, list) else []):
                period = sec.get("period", "")
                title = sec.get("title", "")
                content = sec.get("content", "")
                day_pages_text += f"**{period} — {title}**\n{content}\n\n"
            if diary:
                day_pages_text += f"*AI 的日記：{diary}*\n"
            day_pages_text += "---\n"

        if not day_pages_text:
            day_pages_text = "（沒有日頁面）"

        # 格式化碎片（用于清理操作）
        def _fmt_frag(m):
            res = m.get("resolution", 1.0) or 1.0
            res_tag = f"｜精度{res:.1f}" if res < 1.0 else ""
            return f"- [ID:{m['id']}] 【{m.get('title', '')}】{m['content']}（{str(m.get('created_at', ''))[:10]}{res_tag}）"
        fragments_text = "\n".join(
            _fmt_frag(m) for m in unprocessed
        ) if unprocessed else "（無未處理碎片）"

        scenes_text = "\n".join(
            f"- [场景ID:{s['id']}] 【{s['title']}】{s['narrative'][:200]}..."
            for s in scenes
        ) if scenes else "（暫無記憶場景）"

        permanent_text = "\n".join(
            f"- [ID:{p['id']}] {p.get('title', '')} {p['content']}"
            for p in permanent
        ) if permanent else "（暫無長期設定）"

        # v5.9：格式化老碎片（柔化候选）
        def _fmt_aging(m):
            res = m.get("resolution", 1.0) or 1.0
            ac = m.get("access_count", 0)
            emo = m.get("emotional_weight", 0)
            tags = []
            if res < 1.0:
                tags.append(f"精度{res:.1f}")
            if ac > 0:
                tags.append(f"被召回{ac}次")
            if emo > 0:
                tags.append(f"情緒{emo}")
            tag_str = f"｜{'，'.join(tags)}" if tags else ""
            return f"- [ID:{m['id']}] 【{m.get('title', '')}】{m['content']}（{str(m.get('created_at', ''))[:10]}{tag_str}）"
        aging_text = "\n".join(
            _fmt_aging(m) for m in aging
        ) if aging else "（沒有需要柔化的老碎片）"

        # 3. 构建 prompt（优先用config中的自定义prompt）
        custom_prompt = await get_config("prompt_dream") or ""
        base_prompt = custom_prompt if custom_prompt else DREAM_PROMPT
        prompt = base_prompt.replace("{fragment_count}", str(len(unprocessed)))
        prompt = prompt.replace("{fragments}", fragments_text)
        prompt = prompt.replace("{aging_fragments}", aging_text)
        prompt = prompt.replace("{day_pages}", day_pages_text)
        prompt = prompt.replace("{scenes}", scenes_text)
        prompt = prompt.replace("{profile}", profile)
        prompt = prompt.replace("{permanent}", permanent_text)

        page_count = len(day_pages)
        frag_count = len(unprocessed)
        aging_count = len(aging)
        yield {"type": "progress", "data": f"收集了 {page_count} 個日頁面、{frag_count} 條碎片、{aging_count} 條老碎片、{len(scenes)} 個場景"}

        # 4. 调用模型
        full_narrative = ""
        stats = {
            "memories_processed": len(unprocessed),
            "memories_deleted": 0, "memories_merged": 0, "memories_softened": 0,
            "scenes_created": 0, "scenes_updated": 0,
            "foresights_generated": 0, "links_created": 0,
        }

        # v5.4：动态解析供应商端点
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
                    {"role": "user", "content": "開始做夢。"},
                ],
            }
            _headers, _send_body = prepare_background_request(
                use_api_key, use_api_format, _body,
                referer="https://midsummer-gateway.local", title="Dream",
            )
            async with httpx.AsyncClient(timeout=120) as client:
                response = await client.post(use_api_url, headers=_headers, json=_send_body)

                if response.status_code != 200:
                    error_msg = f"模型請求失敗: HTTP {response.status_code}"
                    await update_dream_log(dream_id, status="error", finished_at=datetime.now(TZ_CST),
                                            dream_narrative=error_msg)
                    yield {"type": "error", "data": error_msg}
                    return

                data = parse_background_response(response.json(), use_api_format)
                text = data.get("choices", [{}])[0].get("message", {}).get("content", "")

        except Exception as e:
            error_msg = f"模型調用出錯: {str(e)}"
            await update_dream_log(dream_id, status="error", finished_at=datetime.now(TZ_CST),
                                    dream_narrative=error_msg)
            yield {"type": "error", "data": error_msg}
            return

        # 5. 解析模型输出，逐段处理
        lines = text.split("\n")
        processed_memory_ids = [m["id"] for m in unprocessed]

        for line in lines:
            if _dream_cancelled:
                await update_dream_log(dream_id, status="interrupted",
                                        finished_at=datetime.now(TZ_CST),
                                        dream_narrative=full_narrative, **stats)
                yield {"type": "narrative", "data": "嗯……？怎么了……"}
                yield {"type": "complete", "data": {"dream_id": dream_id, "interrupted": True, **stats}}
                # 标记已处理的碎片
                await mark_memories_dreamed(processed_memory_ids)
                return

            line = line.strip()
            if not line:
                continue

            if line.startswith("narrative:"):
                narrative_text = line[len("narrative:"):].strip()
                full_narrative += narrative_text + "\n"
                yield {"type": "narrative", "data": narrative_text}

            elif line.startswith("action:"):
                action_text = line[len("action:"):].strip()
                try:
                    action = json.loads(action_text)
                    result = await _execute_dream_action(action, dream_id, stats)
                    yield {"type": "action", "data": result}
                except json.JSONDecodeError:
                    # 尝试从整行提取 JSON
                    import re
                    match = re.search(r'\{.*\}', action_text, re.DOTALL)
                    if match:
                        try:
                            action = json.loads(match.group())
                            result = await _execute_dream_action(action, dream_id, stats)
                            yield {"type": "action", "data": result}
                        except (json.JSONDecodeError, Exception) as e:
                            print(f"   ⚠️ Dream action 解析失敗: {e}")
                    else:
                        # 可能是独白的一部分，当作 narrative 处理
                        full_narrative += action_text + "\n"
                        yield {"type": "narrative", "data": action_text}
            else:
                # 没有前缀的行，当作 narrative
                full_narrative += line + "\n"
                yield {"type": "narrative", "data": line}

        # 6. 标记所有碎片已处理
        await mark_memories_dreamed(processed_memory_ids)

        # 7. 完成
        now = datetime.now(TZ_CST)
        await update_dream_log(dream_id, status="completed", finished_at=now,
                                dream_narrative=full_narrative, **stats)
        await set_config("last_dream_date", now.strftime("%Y-%m-%d"))

        yield {"type": "complete", "data": {"dream_id": dream_id, **stats}}


async def stop_dream():
    """中斷正在進行的 Dream"""
    global _dream_cancelled
    _dream_cancelled = True
    return {"status": "ok", "message": "Dream 中斷訊號已發送"}


# ============================================================
# Dream Action 执行器
# ============================================================

async def _execute_dream_action(action: dict, dream_id: int, stats: dict) -> dict:
    """執行單次 Dream 操作"""
    from database import (
        soft_delete_memories, promote_memory,
        create_mem_scene, update_mem_scene,
    )

    action_type = action.get("type", "")
    result = {"type": action_type, "success": True}

    try:
        if action_type == "delete":
            ids = action.get("memory_ids", [])
            # 确保 ID 是整数（LLM 可能返回字符串）
            ids = [int(i) for i in ids if str(i).isdigit()]
            if ids:
                await soft_delete_memories(ids)
                stats["memories_deleted"] += len(ids)
                result["deleted"] = len(ids)
                result["reason"] = action.get("reason", "")
                print(f"   🧹 刪除 {len(ids)} 條碎片: {action.get('reason', '')}")

        elif action_type == "merge":
            ids = action.get("memory_ids", [])
            # 确保 ID 是整数（LLM 可能返回字符串）
            ids = [int(i) for i in ids if str(i).isdigit()]
            if ids:
                # 软删除被合併的碎片
                await soft_delete_memories(ids)
                stats["memories_merged"] += len(ids)
                # 创建合併后的新记忆
                from database import save_memory, get_embedding
                merged = action.get("merged_content", "")
                title = action.get("merged_title", "")
                new_merge_id = None
                if merged:
                    embedding = await get_embedding(f"{title} {merged}" if title else merged)
                    embedding_json = json.dumps(embedding) if embedding else None
                    from database import get_pool
                    pool = await get_pool()
                    async with pool.acquire() as conn:
                        new_merge_id = await conn.fetchval("""
                            INSERT INTO memories (content, title, importance, memory_type, embedding, source, source_session, dream_processed_at)
                            VALUES ($1, $2, 6, 'daily_digest', $3, 'dream_merge', 'dream', NOW())
                            RETURNING id
                        """, merged, title, embedding_json)
                result["merged"] = len(ids)
                if new_merge_id:
                    result["new_id"] = new_merge_id
                    print(f"   🔗 合併 {len(ids)} 條碎片 → #{new_merge_id} {title}")
                else:
                    print(f"   ⚠️ 合併 {len(ids)} 條碎片但 merged_content 為空，未創造新記憶")

        elif action_type == "promote":
            mid = action.get("memory_id")
            if mid is not None:
                mid = int(mid)
                await promote_memory(mid)
                result["memory_id"] = mid
                print(f"   ⭐ 升格記憶 #{mid}: {action.get('reason', '')}")

        elif action_type == "soften":
            mid = action.get("memory_id")
            softened_content = action.get("softened_content", "")
            target_resolution = action.get("target_resolution", 0.5)
            if mid is not None and softened_content:
                mid = int(mid)
                from database import soften_memory
                success = await soften_memory(
                    memory_id=mid,
                    softened_content=softened_content,
                    target_resolution=float(target_resolution),
                    extend_days=30,
                )
                if success:
                    stats["memories_softened"] = stats.get("memories_softened", 0) + 1
                    result["memory_id"] = mid
                    result["resolution"] = target_resolution
                    result["reason"] = action.get("reason", "")
                else:
                    result["skipped"] = "soften failed (locked, not found, or already softer)"

        elif action_type == "create_scene":
            scene_id = await create_mem_scene(
                title=action.get("title", "未命名場景"),
                narrative=action.get("narrative", ""),
                atomic_facts=action.get("atomic_facts", []),
                foresight=action.get("foresight", []),
                related_memory_ids=action.get("related_memory_ids", []),
                dream_id=dream_id,
            )
            stats["scenes_created"] += 1
            foresight_count = len(action.get("foresight", []))
            stats["foresights_generated"] += foresight_count
            result["scene_id"] = scene_id
            result["title"] = action.get("title", "")
            print(f"   🧩 新建場景 #{scene_id}: {action.get('title', '')}")
            if foresight_count:
                print(f"   🔮 生成 {foresight_count} 條前瞻信號")

        elif action_type == "update_scene":
            sid = action.get("scene_id")
            if sid:
                updates = {}
                for key in ("narrative", "atomic_facts", "foresight"):
                    if key in action:
                        updates[key] = action[key]
                if updates:
                    await update_mem_scene(sid, **updates)
                    stats["scenes_updated"] += 1
                    result["scene_id"] = sid
                    print(f"   📝 更新場景 #{sid}")

        elif action_type == "update_profile":
            # 画像更新暂时只记录，不自动执行（留给日常画像更新流程）
            result["note"] = "profile update logged, will apply in next daily update"
            print(f"   🪞 畫像更新建議: {action.get('section', '')} - {action.get('content', '')[:50]}")

        elif action_type == "link":
            from_id = action.get("from_id")
            to_id = action.get("to_id")
            edge_type = action.get("edge_type", "references")
            if from_id is not None and to_id is not None:
                try:
                    from_id = int(from_id)
                    to_id = int(to_id)
                except (ValueError, TypeError):
                    result["skipped"] = "invalid ID format"
                    return result
                from_type = action.get("from_type", "memory")
                to_type = action.get("to_type", "memory")
                reason = action.get("reason", "")
                from database import create_memory_edge, invalidate_memory

                created = await create_memory_edge(
                    from_id, from_type, to_id, to_type, edge_type,
                    reason=reason, created_by="dream", validate_ids=True
                )
                if not created:
                    result["skipped"] = "ID not found or edge already exists"
                    return result

                # v5.3：supersedes/contradicts 时自动标旧记忆失效
                if edge_type in ("supersedes", "contradicts") and to_type == "memory":
                    await invalidate_memory(to_id, reason=f"Dream 標記 {edge_type} by #{from_id}")

                result["from_id"] = from_id
                result["to_id"] = to_id
                result["edge_type"] = edge_type
                stats["links_created"] += 1

    except Exception as e:
        result["success"] = False
        result["error"] = str(e)
        print(f"   ⚠️ Dream action 執行失敗: {e}")

    return result


# ============================================================
# 犯困检测 — 在聊天时注入 system prompt
# ============================================================

async def get_drowsy_prompt() -> str:
    """
    檢查 AI 是否該犯困了，返回要注入的 system prompt 片段。
    空字符串 = 不困。
    
    三個條件任一滿足就犯困：
    1. 未處理碎片 >= 30 條
    2. 距離上次Dream超過7天
    3. 有3天以上的日頁面未被Dream處理
    """
    from config import get_config
    from database import get_unprocessed_memories, get_pool

    last_dream = await get_config("last_dream_date")
    drowsy_threshold = int(await get_config("dream_drowsy_threshold") or "30")

    # 条件1：未处理碎片数量
    unprocessed = await get_unprocessed_memories()
    fragment_count = len(unprocessed)
    too_many_fragments = fragment_count >= drowsy_threshold

    # 条件2：距上次Dream超过7天
    days_since_dream = 0
    too_long_no_dream = False
    if last_dream:
        try:
            from datetime import date as date_cls
            last = date_cls.fromisoformat(last_dream)
            today = datetime.now(TZ_CST).date()
            days_since_dream = (today - last).days
            too_long_no_dream = days_since_dream >= 7
        except Exception:
            too_long_no_dream = True
    else:
        # 从来没Dream过，也算该睡了
        too_long_no_dream = True

    # 条件3：有3天以上日页面未被Dream处理
    unprocessed_pages = 0
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            # 数日页面中日期晚于上次Dream的数量
            if last_dream:
                from datetime import date as date_cls2
                last_d = date_cls2.fromisoformat(last_dream)
                count = await conn.fetchval("""
                    SELECT COUNT(*) FROM calendar_pages
                    WHERE type = 'day' AND date > $1
                """, last_d)
            else:
                count = await conn.fetchval(
                    "SELECT COUNT(*) FROM calendar_pages WHERE type = 'day'"
                )
            unprocessed_pages = count or 0
    except Exception:
        pass
    too_many_pages = unprocessed_pages >= 3

    # 三个条件都不满足 = 不困
    if not too_many_fragments and not too_long_no_dream and not too_many_pages:
        return ""

    # 构建犯困提示
    reasons = []
    if too_many_fragments:
        reasons.append(f"腦子裡堆了 {fragment_count} 條記憶碎片還沒整理")
    if too_long_no_dream:
        if last_dream and days_since_dream > 0:
            reasons.append(f"已經連續 {days_since_dream} 天沒有睡覺了")
        else:
            # 从未做过梦 / 上次时间无法解析
            reasons.append("從來都沒好好睡過一覺")
    if too_many_pages:
        reasons.append(f"有 {unprocessed_pages} 天的日記還沒消化")

    reason_text = "，".join(reasons) + "。"

    return f"""
【AI 的狀態】
{reason_text}有些東西開始攪在一起了。
你感到很困，偶爾可以在對話中自然地跟使用者提一下你困了、想睡一下。
不要每句話都說困，只是偶爾自然提一下。如果使用者說"去睡覺吧"，你可以點點頭表示。 """


# ============================================================
# 自动 Dream — 24小时无活动时触发
# ============================================================

async def auto_dream_check():
    """
    檢查是否需要自動觸發 Dream（24小時無活動）
    由定時任務每小時呼叫一次
    """
    from config import get_config
    from database import get_pool, get_unprocessed_memories

    # 跳过 0:00-1:00 时段，避免与 daily_digest_scheduler（0:05）竞争
    now_hour = datetime.now(TZ_CST).hour
    if now_hour == 0:
        return False

    # 检查上次活动时间
    pool = await get_pool()
    async with pool.acquire() as conn:
        last_msg = await conn.fetchval("""
            SELECT MAX(time) FROM chat_messages WHERE role = 'user'
        """)

    if not last_msg:
        return False

    now = datetime.now(TZ_CST)
    if hasattr(last_msg, "astimezone"):
        last_msg = last_msg.astimezone(TZ_CST)

    hours_since = (now - last_msg).total_seconds() / 3600

    if hours_since < 24:
        return False

    # 用综合条件判断是否值得Dream
    unprocessed = await get_unprocessed_memories()
    fragment_count = len(unprocessed)

    last_dream = await get_config("last_dream_date")
    days_since_dream = 0
    if last_dream:
        try:
            from datetime import date as date_cls
            last = date_cls.fromisoformat(last_dream)
            days_since_dream = (now.date() - last).days
        except Exception:
            days_since_dream = 999

    # 数未被Dream处理的日页面
    unprocessed_pages = 0
    try:
        async with pool.acquire() as conn:
            if last_dream:
                from datetime import date as date_cls2
                last_d = date_cls2.fromisoformat(last_dream)
                count = await conn.fetchval(
                    "SELECT COUNT(*) FROM calendar_pages WHERE type = 'day' AND date > $1", last_d
                )
            else:
                count = await conn.fetchval(
                    "SELECT COUNT(*) FROM calendar_pages WHERE type = 'day'"
                )
            unprocessed_pages = count or 0
    except Exception:
        pass

    # 三个条件都不满足 = 不值得Dream
    should_dream = (fragment_count >= 5) or (days_since_dream >= 7) or (unprocessed_pages >= 3)
    if not should_dream:
        return False

    # 检查是否已经在Dream
    if _dream_lock.locked():
        return False

    print(f"🌙 自動Dream觸發：使用者 {hours_since:.0f}h 未活動 | {fragment_count} 條碎片 | {days_since_dream} 天未Dream | {unprocessed_pages} 个日页面未处理")

    # 静默执行 Dream（不通过SSE，直接跑完）
    async for event in run_dream(trigger_type="auto"):
        if event["type"] == "narrative":
            pass  # 静默，不输出
        elif event["type"] == "error":
            print(f"   ⚠️ 自動Dream出錯: {event['data']}")
        elif event["type"] == "complete":
            print(f"   ✅ 自動Dream完成: {event['data']}")

    return True


async def auto_dream_scheduler():
    """
    後台定時任務：每小時檢查一次是否需要自動 Dream
    """
    print("🌙 自動Dream檢查器已啟動（每小時檢查一次）")
    while True:
        try:
            await asyncio.sleep(3600)  # 每小時
            await auto_dream_check()
        except asyncio.CancelledError:
            print("🌙 自動Dream檢查器已停止")
            break
        except Exception as e:
            print(f"⚠️ 自動Dream檢查出錯: {e}")
            await asyncio.sleep(300)
