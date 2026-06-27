"""
config.py — 動態配置管理（v3.1）

配置優先權：資料庫 > 環境變數 > 預設值
修改後即時生效，不需要重新啟動服務

設定表 gateway_config 的建表由 database.py 的 init_tables() 負責
"""

import os
from typing import Optional
from database import get_pool


# ============================================================
# 配置项定义
# ============================================================
# key → (环境变量名, 默认值, 中文标签, 值类型)

CONFIG_SCHEMA = {
    "memory_enabled":        ("MEMORY_ENABLED",         "true",  "記憶系統開關",      "bool"),
    "extract_interval":      ("MEMORY_EXTRACT_INTERVAL", "5",    "提取間隔（輪）",    "int"),
    "max_inject":            ("MAX_MEMORIES_INJECT",     "15",   "每次注入條數",      "int"),
    "semantic_threshold":    ("SEMANTIC_THRESHOLD",      "0.25", "語義搜索閾值",      "float"),
    "dedup_threshold":       ("DEDUP_THRESHOLD",         "0.55", "去重相似度閾值",    "float"),
    "scene_inject_enabled":  ("SCENE_INJECT_ENABLED",  "true", "場景注入開關",      "bool"),
    "scene_inject_limit":    ("SCENE_INJECT_LIMIT",    "2",    "場景注入條數",      "int"),
    "scene_inject_min_sim":  ("SCENE_INJECT_MIN_SIM",  "0.5",  "場景注入相似度閾值", "float"),
    # 默认模型配置（v3.7）
    "default_chat_model":    ("DEFAULT_MODEL",           "",     "預設聊天模型",      "text"),
    "default_title_model":   ("",                        "",     "標題總結模型",      "text"),
    "default_memory_model":  ("MEMORY_MODEL",            "",     "記憶提取模型",      "text"),
    "default_digest_model":  ("",                        "",     "每日整理模型",      "text"),
    "default_embedding_model":("EMBEDDING_MODEL",        "",     "嵌入模型",          "text"),
    # 提示词模板（v3.7）
    "prompt_title_summary":  ("",                        "",     "標題總結提示詞",    "text"),
    "prompt_memory_extract": ("",                        "",     "記憶提取提示詞",    "text"),
    "prompt_daily_digest":   ("",                        "",     "每日整理提示詞",    "text"),
    # 上下文压缩（v3.9）
    "default_compress_model":("",                        "",     "上下文壓縮模型",    "text"),
    "prompt_compress":       ("",                        "",     "上下文壓縮提示詞",  "text"),
    # 用户画像（v4.0）
    "user_profile":          ("",                        "",     "使用者畫像",          "text"),
    "prompt_user_profile":   ("",                        "",     "畫像更新提示詞",    "text"),
    # Dream 记忆整合（v5.1）
    "dream_model":           ("",                        "",     "Dream 模型",        "text"),
    "prompt_dream":          ("",                        "",     "Dream 提示詞",      "text"),
    "prompt_daily_digest_page":("",                      "",     "日頁面生成提示詞",  "text"),
    "prompt_weekly_summary": ("",                        "",     "周總結提示詞",      "text"),
    "prompt_monthly_summary":("",                        "",     "月總結提示詞",      "text"),
    "prompt_period_summary": ("",                        "",     "季度/年總結提示詞", "text"),
    "dream_drowsy_threshold":("",                        "30",   "犯困碎片閾值",      "int"),
    "last_dream_date":       ("",                        "",     "上次 Dream 日期",   "text"),
    # v5.9：记忆新陈代谢
    "auto_soften_enabled":   ("AUTO_SOFTEN_ENABLED",   "true", "自動柔化開關",      "bool"),
    "auto_soften_daily_limit":("AUTO_SOFTEN_DAILY_LIMIT","10",   "每日柔化上限",      "int"),
    "auto_soften_min_age":   ("AUTO_SOFTEN_MIN_AGE",   "5",    "自動柔化最小天數",  "int"),
    "soften_cooldown_days":  ("SOFTEN_COOLDOWN_DAYS",  "21",   "柔化冷却天數",      "int"),
    "lock_retire_enabled":   ("LOCK_RETIRE_ENABLED",   "true", "自動鎖定退役開關",  "bool"),
    "lock_retire_days":      ("LOCK_RETIRE_DAYS",      "90",   "鎖定退役天數",      "int"),
    # v5.5：日历层级注入
    "calendar_inject_enabled":("",                       "true", "日曆注入開關",      "bool"),
    # v5.5：Prompt 缓存（Claude 模型省 90% 输入费用）
    "prompt_cache_enabled":   ("",                       "true", "Prompt 快取",      "bool"),
    # v5.6：无缝切窗（新对话衔接上一个对话的上下文）
    "handoff_enabled":        ("",                       "true", "對話銜接開關",      "bool"),
    "handoff_msg_count":      ("",                       "6",    "銜接注入條數",      "int"),
    "handoff_stop_rounds":    ("",                       "3",    "銜接停止輪數",      "int"),
    "handoff_summary_model":  ("",                       "",     "銜接摘要模型",      "text"),
    "prompt_handoff_summary": ("",                       "",     "銜接摘要提示詞",    "text"),
    # v5.4：热度系统参数（从代码硬编码提取为可配置）
    "heat_half_life_normal": ("",                        "3",    "普通記憶半衰期（天）",  "float"),
    "heat_half_life_important":("",                      "7",    "重要記憶半衰期（天）",  "float"),
    "heat_recall_extend":    ("",                        "0.5",  "召回延長半衰期倍率",    "float"),
    "heat_threshold_high":   ("",                        "0.7",  "高活躍度閾值（全文注入）", "float"),
    "heat_threshold_medium": ("",                        "0.3",  "中活躍度閾值（摘要注入）", "float"),
    "heat_importance_line":  ("",                        "8",    "重要度分界線",          "int"),
    "heat_emotion_line":     ("",                        "6",    "高情緒分界線",          "int"),
    "heat_medium_truncate":  ("",                        "60",   "中活躍度摘要截斷字數",    "int"),
    "cleanup_heat_threshold":("CLEANUP_HEAT_THRESHOLD", "0.15", "清理低活躍度閾值",    "float"),
    "merge_retention_days":  ("MERGE_RETENTION_DAYS",  "90",   "合併記憶保留天數",  "int"),
    "merge_min_keep":        ("MERGE_MIN_KEEP",        "20",   "合併記憶保留下限",  "int"),
    # v5.4：记忆自动锁定
    "autolock_access_count": ("",                        "10",   "自動鎖定：召回次數閾值",   "int"),
    "autolock_diversity":    ("",                        "5",    "自動鎖定：話題多樣性閾值", "int"),
    "autolock_emo_access":   ("",                        "6",    "自動鎖定：高情緒召回閾值", "int"),
    "autolock_emo_diversity": ("",                       "3",    "自動鎖定：高情緒多樣性閾值","int"),
    # 联网搜索配置（v3.8）
    "search_engine":         ("SEARCH_ENGINE",           "",     "搜尋引擎",          "text"),
    "search_api_key":        ("SEARCH_API_KEY",          "",     "搜尋 API Key",      "text"),
    "search_max_results":    ("SEARCH_MAX_RESULTS",      "5",    "搜尋結果條數",      "int"),
    # 云端同步 — 用户/助手配置（v4.1）
    "user_avatar":           ("",                        "",     "使用者頭像",          "text"),
    "user_nickname":         ("",                        "",     "使用者暱稱",          "text"),
    "assistant_avatar":      ("",                        "",     "助手頭像",          "text"),
    "assistant_settings":    ("",                        "",     "助手參數",          "text"),
    "custom_skills":         ("",                        "",     "自訂技能",        "text"),
    "quick_phrases":         ("",                        "",     "常用語",          "text"),
    "mcp_switches":          ("",                        "",     "MCP開關狀態",       "text"),
    "mcp_servers":           ("",                        "",     "MCP伺服器列表",     "text"),
    "mcp_manual_ids":        ("",                        "",     "手動MCP選擇",       "text"),
    "mcp_mode":              ("",                        "auto", "MCP模式",           "text"),
    "ext_drawer_threshold":  ("EXT_DRAWER_THRESHOLD",   "0.40", "外部抽屉相似度閾值", "float"),
    "ext_drawer_max_open":   ("EXT_DRAWER_MAX_OPEN",    "3",    "外部抽屉同開上限",   "int"),
    "theme_preference":      ("",                        "",     "主題偏好",          "text"),
    # v6.3：工具抽屉（向量路由按需展开工具）。默认关闭——开启后内部工具走向量路由，
    #       外部 mcp_servers 仍走原路径并合并，对模型表现为一组完整工具
    "tool_drawer_enabled":   ("",                        "false","工具抽屜開關",      "bool"),
}


# ============================================================
# 读取配置
# ============================================================

def _env_or_default(env_name: str, default_val: str) -> tuple:
    """解析"环境变量 > 默认值"。

    环境变量未设置、或被设成空串 / 纯空白时一律视为"未设置"，回落到默认值，
    避免空环境变量（如 docker-compose 里 KEY=${KEY} 而 KEY 未定义）把默认值冲掉。
    返回 (值, 来源)，来源为 'env' 或 'default'。
    """
    if env_name:
        env_val = os.getenv(env_name)
        if env_val is not None and env_val.strip() != "":
            return env_val, "env"
    return default_val, "default"


async def get_config(key: str) -> Optional[str]:
    """
    取得單一配置值
    優先權：資料庫 > 環境變數 > 預設值
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT value FROM gateway_config WHERE key = $1", key
        )
        if row:
            return row["value"]
    
    # 降级到环境变量和默认值
    if key in CONFIG_SCHEMA:
        env_name, default_val, _, _ = CONFIG_SCHEMA[key]
        value, _ = _env_or_default(env_name, default_val)
        return value
    
    return None


async def get_all_config() -> dict:
    """取得所有配置（合併資料庫、環境變數、預設值）"""
    result = {}
    
    # 先填默认值和环境变量
    for key, (env_name, default_val, label, val_type) in CONFIG_SCHEMA.items():
        env_val, source = _env_or_default(env_name, default_val)
        result[key] = {
            "value": env_val,
            "label": label,
            "type": val_type,
            "source": source,
        }
    
    # 覆盖数据库里的值
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT key, value FROM gateway_config")
        for row in rows:
            if row["key"] in result:
                result[row["key"]]["value"] = row["value"]
                result[row["key"]]["source"] = "database"
    
    return result


# ============================================================
# 写入配置
# ============================================================

async def set_config(key: str, value: str) -> bool:
    """設定配置值（存入資料庫，帶類型驗證）"""
    if key not in CONFIG_SCHEMA:
        return False
    
    # 类型验证
    _, _, _, val_type = CONFIG_SCHEMA[key]
    try:
        if val_type == "int":
            int(value)
        elif val_type == "float":
            float(value)
        elif val_type == "bool":
            if value.lower() not in ("true", "false"):
                return False
        # text 类型不需要验证
    except ValueError:
        return False
    
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO gateway_config (key, value, label, updated_at)
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = NOW()
        """, key, value, CONFIG_SCHEMA[key][2])
    
    print(f"⚙️  配置更新: {key} = {value}")
    return True


# ============================================================
# 类型便捷读取
# ============================================================

async def get_config_int(key: str, fallback: int = 0) -> int:
    """取得整數配置"""
    val = await get_config(key)
    try:
        return int(val) if val else fallback
    except (ValueError, TypeError):
        return fallback


async def get_config_float(key: str, fallback: float = 0.0) -> float:
    """取得浮點數配置"""
    val = await get_config(key)
    try:
        return float(val) if val else fallback
    except (ValueError, TypeError):
        return fallback


async def get_config_bool(key: str, fallback: bool = False) -> bool:
    """获取布尔配置（接受 true/1/yes/on 与 false/0/no/off 等常见写法）"""
    val = await get_config(key)
    if val is None:
        return fallback
    v = val.strip().lower()
    if v in ("true", "1", "yes", "on"):
        return True
    if v in ("false", "0", "no", "off"):
        return False
    return fallback
