"""
預置記憶導入範例
==================
把你想讓 AI 從一開始就"記住"的事情寫在這裡。
部署後造訪 /import/seed-memories 即可一次導入。

使用方法：
1. 複製此檔案為 seed_memories.py
2. 修改 SEED_MEMORIES 列表
3. 部署後存取 /import/seed-memories

importance 評分規則（1-10）：
- 9-10: 核心身分資訊（名字、關係、最重要的承諾）
- 7-8: 重要偏好與習慣（飲食、作息、工作）
- 5-6: 有趣的事件和細節
- 3-4: 臨時性訊息
"""

from database import get_pool, save_memory, get_all_memories_count

SEED_MEMORIES = [
    # ======== 基础信息（改成你自己的） ========
    {"content": "用戶的名字是小明", "importance": 9},
    {"content": "用戶養了一只橘貓叫大橘", "importance": 7},
    {"content": "用戶是程式設計師，主要寫 Python", "importance": 7},
    {"content": "用戶住在北京，喜歡吃火鍋", "importance": 6},
    
    # ======== 偏好 ========
    {"content": "用戶喜歡簡潔的回答，不喜歡太囉嗦", "importance": 8},
    {"content": "用戶是 INTJ，喜歡邏輯清晰的討論", "importance": 6},
    
    # ======== 重要事件 ========
    {"content": "2026-01-01 用戶和 AI 開始使用記憶系統", "importance": 7},
    
    # ======== 在这里继续添加更多记忆 ========
]


async def run_seed_import():
    """執行導入（自動跳過已存在的記憶）"""
    pool = await get_pool()
    before = await get_all_memories_count()
    
    imported = 0
    skipped = 0
    
    for mem in SEED_MEMORIES:
        async with pool.acquire() as conn:
            existing = await conn.fetchval(
                "SELECT COUNT(*) FROM memories WHERE content = $1",
                mem["content"],
            )
        
        if existing > 0:
            skipped += 1
            continue
        
        await save_memory(
            content=mem["content"],
            importance=mem["importance"],
            source_session="seed-import",
        )
        imported += 1
    
    after = await get_all_memories_count()
    
    return {
        "status": "done",
        "imported": imported,
        "skipped": skipped,
        "before": before,
        "after": after,
    }
