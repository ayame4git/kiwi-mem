"""
記憶擷取模組 —— 用 LLM 從對話中精進關鍵記憶
=============================================
每次對話結束後，把最近的對話內容發給一個便宜的模型，
讓它提取出值得記住的信息，存到資料庫裡。

v2.3 改進：提取時注入已有記憶，讓模型對比後只提取全新資訊。
"""

import os
import json
import httpx
from typing import List, Dict

API_KEY = os.getenv("MEMORY_API_KEY", "") or os.getenv("API_KEY", "")
_RAW_BASE_URL = os.getenv("MEMORY_API_BASE_URL", "") or os.getenv("API_BASE_URL", "https://openrouter.ai/api/v1/chat/completions")

# 确保 URL 以 /chat/completions 结尾
API_BASE_URL = _RAW_BASE_URL if _RAW_BASE_URL.rstrip("/").endswith("/chat/completions") else f"{_RAW_BASE_URL.rstrip('/')}/chat/completions"

# 用来提取记忆的模型（便宜的就行）
MEMORY_MODEL = os.getenv("MEMORY_MODEL", "anthropic/claude-haiku-4")


EXTRACTION_PROMPT = """你是資訊提取專家，負責從對話中識別並提取值得長期記住的關鍵資訊。

# 提取重點
- 關鍵訊息：僅提取用戶的重要訊息，忽略日常瑣事
- 重要事件：記憶深刻的互動，需包含人物、時間、地點（如有）

# 提取範圍
- 個人：年齡、生日、職業、學歷、居住地
- 偏好：明確表達的喜好或厭惡
- 健康：身體狀況、過敏史、飲食禁忌
- 事件：與AI的重要互動、約定、里程碑
- 關係：家人、朋友、重要同事
- 價值觀：表達的信念或長期目標
- 情感：重要的情感時刻或關係里程碑

{emotion_instruction}

# 不要提取
- 日常寒暄（"你好""在嗎"）
- AI的一般回應、長篇論述和解釋說明（但 AI 所做的承諾、約定、重要表態、對關係有意義的話需要提取）
- 關於記憶系統本身的討論（"某記憶沒有被記錄""記憶遺漏""沒有被提取"等）
- 技術調試、bug修復的過程性討論（除非涉及用戶技能或專案里程碑）
- AI的思考過程、思考鏈內容

# 已知資訊處理【最重要】
<已知資訊>
{existing_memories}
</已知資訊>

- 新資訊必須與已知資訊逐條比對
- 相同、相似或語意重複的資訊必須忽略（例如已知"用戶去媽媽家吃團年飯"，就不要再提取"用戶春節去了媽媽家"）
- 已知資訊的補充或更新可以提取（例如已知"用戶養了一隻貓"，新資訊"貓最近生病了"可以提取）
- 與已知資訊矛盾的新資訊可以提取（標註為更新）
- 僅提取完全新增且不與已知資訊重複的內容
- 如果對話中沒有任何新訊息，回傳空數組 []
# 可用的分類列表
{categories_list}

# 輸出格式
請用以下 JSON 格式回傳（不要包含其他內容）：
[
  {"title": "簡短標題", "content": "記憶內容", "importance": 分數, "emotional_weight": 情緒濃度, "category": "分類名"},
  {"title": "簡短標題", "content": "記憶內容", "importance": 分數, "emotional_weight": 情緒濃度, "category": "分類名"},
]

字段說明：
- title: 用4-10個字概括這記憶的主題（如"飲食偏好""用藥方案""情緒里程碑"）
- content: 記憶的具體內容
- importance: 訊息重要度 1-10，10 最重要
- emotional_weight: 情緒濃度 0-10，0=無情緒，10=極強情緒。判斷標準是對話時雙方的情緒強度，不是訊息重要性
- category: 從上面的分類清單中選擇最合適的一個，如果都不合適就填空字串 ""
如果沒有值得記住的新訊息，回傳空數組：[]
"""

# 高情绪时追加的提取指引
EMOTION_HIGH_INSTRUCTION = """# 🩷 情緒錨點提取【本輪對話情緒濃度高，請特別注意】
本輪對話被偵測到情緒濃度較高。除了資訊性記憶外，還要辨識以下內容：
- 使用者表達了強烈情緒的時刻（哭泣、崩潰、特別開心、深層信任、脆弱暴露）
- AI的回應讓使用者情緒發生明顯變化的時刻
- 即使沒有"新資訊"，只要情緒濃度高，也值得提取
- 這類記憶的 emotional_weight 應為 6-10"""

EMOTION_NORMAL_INSTRUCTION = ""


async def extract_memories(messages: List[Dict[str, str]], existing_memories: List[str] = None, categories: List[str] = None, model_override: str = None, prompt_override: str = None, emotion_level: str = "normal") -> List[Dict]:
    """
    從對話訊息中提取記憶

    參數：
        messages: 對話訊息列表，格式 [{"role": "user", "content": "..."}, ...]
        existing_memories: 已有記憶內容列表，用於去重對比
        categories: 可用的分類名稱列表，用於自動歸類
        model_override: 覆蓋預設提取模型（從數據庫配置傳入）
        prompt_override: 覆蓋預設提取提示詞（從數據庫配置傳入）
        emotion_level: 本輪對話的情緒層次（'high'/'medium'/'normal'），影響提取策略

    回傳：
        記憶列表，格式 [{"content": "...", "importance": N, "emotional_weight": N, "category": "..."}, ...]
    """
    if not messages:
        return []

    # 把对话格式化成文本
    conversation_text = ""
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if role == "user":
            conversation_text += f"用户: {content}\n"
        elif role == "assistant":
            conversation_text += f"AI: {content}\n"

    if not conversation_text.strip():
        return []

    # 格式化已有记忆
    if existing_memories:
        memories_text = "\n".join(f"- {m}" for m in existing_memories)
    else:
        memories_text = "（暫無已知資訊）"

    # 格式化分类列表
    if categories:
        categories_text = "、".join(categories)
    else:
        categories_text = "（暫無分類，category 字段填空字串即可）"

    # 把已有记忆和分类填入prompt（用 replace 而非 format，防止 prompt 里的花括号被误解析）
    base_prompt = prompt_override if prompt_override else EXTRACTION_PROMPT
    
    # 注入情绪指引（v5.2）
    emotion_instruction = EMOTION_HIGH_INSTRUCTION if emotion_level == "high" else EMOTION_NORMAL_INSTRUCTION
    
    prompt = (base_prompt
        .replace("{existing_memories}", memories_text)
        .replace("{categories_list}", categories_text)
        .replace("{emotion_instruction}", emotion_instruction)
    )

    # 确定使用的模型
    use_model = model_override if model_override else MEMORY_MODEL

    # v5.4：动态解析供应商端点（优先走数据库 provider，降级到环境变量）
    try:
        from database import resolve_model_endpoint
        use_api_url, use_api_key, use_api_format = await resolve_model_endpoint(use_model)
    except Exception:
        use_api_url = API_BASE_URL
        use_api_key = API_KEY
        use_api_format = "openai"

    if not use_api_key:
        print("⚠️  無可用 API Key（供應商和環境變數均未配置），跳過記憶提取")
        return []

    # 调用 LLM 提取记忆
    try:
        from anthropic_adapter import prepare_background_request, parse_background_response
        _body = {
            "model": use_model,
            "max_tokens": 1000,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": f"請從以下對話中提取新的記憶：\n\n{conversation_text}"},
            ],
        }
        _headers, _send_body = prepare_background_request(
            use_api_key, use_api_format, _body,
            referer="https://midsummer-gateway.local",
            title="AI Memory Gateway - Memory Extraction",
        )
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(use_api_url, headers=_headers, json=_send_body)

            if response.status_code != 200:
                print(f"⚠️  記憶提取請求失敗: {response.status_code}")
                return []

            data = parse_background_response(response.json(), use_api_format)
            text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            
            # 日志：打印模型原始回傳（方便排查）
            print(f"🔍 記憶提取模型回傳（前200字）: {text[:200]}...")

            # 清理可能的 markdown 格式
            text = text.strip()
            if text.startswith("```json"):
                text = text[7:]
            if text.startswith("```"):
                text = text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

            # 解析 JSON（正则兜底：从文本中提取 JSON 数组）
            memories = None
            try:
                memories = json.loads(text)
            except json.JSONDecodeError:
                # 兜底：用正则找 [ ... ] 部分
                import re
                match = re.search(r'\[.*\]', text, re.DOTALL)
                if match:
                    try:
                        memories = json.loads(match.group())
                        print(f"🔧 JSON 正規兜底解析成功")
                    except json.JSONDecodeError:
                        pass
            
            if not memories or not isinstance(memories, list):
                print(f"⚠️  記憶提取回傳非数组格式，跳過")
                return []

            # 验证格式
            valid_memories = []
            for mem in memories:
                if isinstance(mem, dict) and "content" in mem:
                    # importance 安全转换：LLM 可能回傳浮点、字符串或 null
                    try:
                        imp = int(float(mem.get("importance", 5)))
                        imp = max(1, min(10, imp))
                    except (ValueError, TypeError):
                        imp = 5
                    # emotional_weight 安全转换（v5.2）
                    try:
                        emo = int(float(mem.get("emotional_weight", 0)))
                        emo = max(0, min(10, emo))
                    except (ValueError, TypeError):
                        emo = 0
                    valid_memories.append({
                        "title": str(mem.get("title", "")),
                        "content": str(mem["content"]),
                        "importance": imp,
                        "emotional_weight": emo,
                        "category": str(mem.get("category", "")),
                    })

            print(f"📝 從對話中提取了 {len(valid_memories)} 個新記憶（已對比 {len(existing_memories or [])} 條已有記憶）")
            return valid_memories

    except json.JSONDecodeError as e:
        print(f"⚠️  記憶提取結果解析失敗: {e}")
        return []
    except Exception as e:
        print(f"⚠️  記憶提取出錯: {e}")
        return []
