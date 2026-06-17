"""
Gateway usage logger — writes one row per chat/completion to data.gateway_usage.
Bind-mount into kiwi-mem container as /app/gateway_usage.py.
"""
import os
import httpx
from datetime import datetime, timezone

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_KEY", "")
GATEWAY_USAGE_DISABLED = os.getenv("GATEWAY_USAGE_DISABLED", "").lower() in ("1", "true", "yes")

_pricing_cache: dict = {}

def _provider_from_model(model: str) -> str:
    m = (model or "").lower()
    if m.startswith("claude"):
        return "anthropic"
    if m.startswith("gemini"):
        return "gemini"
    if m.startswith("gpt") or m.startswith("o1") or m.startswith("o3") or m.startswith("o4"):
        return "openai"
    return "unknown"

async def _load_pricing(client: httpx.AsyncClient) -> dict:
    global _pricing_cache
    if _pricing_cache:
        return _pricing_cache
    if not SUPABASE_URL or not SUPABASE_KEY:
        return {}
    r = await client.get(
        f"{SUPABASE_URL}/rest/v1/model_pricing?select=*",
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Accept-Profile": "data",
        },
        timeout=5,
    )
    if r.status_code == 200:
        for row in r.json():
            _pricing_cache[row["model"]] = row
    return _pricing_cache

def _compute_cost(pricing: dict, usage: dict) -> tuple:
    """Returns (cost_usd, cost_save_usd). All numbers in USD."""
    if not pricing:
        return (None, None)
    pt = float(usage.get("prompt_tokens") or 0)
    ct = float(usage.get("completion_tokens") or 0)
    # Anthropic-style cache fields. OpenAI/Gemini may not have these.
    cache_read = float(
        usage.get("cache_read_input_tokens")
        or (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
        or 0
    )
    cache_write = float(usage.get("cache_creation_input_tokens") or 0)
    # Regular input = prompt_tokens - cache_read - cache_write (Anthropic exposes the split this way)
    regular_in = max(0.0, pt - cache_read - cache_write)
    in_p = float(pricing.get("input_per_mtok") or 0)
    out_p = float(pricing.get("output_per_mtok") or 0)
    cr_p = float(pricing.get("cache_read_per_mtok") or 0)
    cw_p = float(pricing.get("cache_write_per_mtok") or 0)
    cost = (regular_in * in_p + cache_read * cr_p + cache_write * cw_p + ct * out_p) / 1_000_000.0
    # Savings: what cache_read would have cost at full input rate, minus what it actually cost
    save = (cache_read * (in_p - cr_p)) / 1_000_000.0 if cache_read > 0 else 0.0
    return (round(cost, 6), round(save, 6))

async def log_usage(model: str, usage: dict | None, via: str = "chat",
                    request_id: str | None = None) -> None:
    """
    Fire-and-forget logging. Never raises.
    """
    if GATEWAY_USAGE_DISABLED:
        return
    if not usage or not model:
        return
    if not SUPABASE_URL or not SUPABASE_KEY:
        return
    try:
        provider = _provider_from_model(model)
        async with httpx.AsyncClient(timeout=5) as client:
            pricing_map = await _load_pricing(client)
            pricing = pricing_map.get(model, {})
            cost_usd, cost_save_usd = _compute_cost(pricing, usage)
            cache_read = (usage.get("cache_read_input_tokens")
                          or (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
                          or 0)
            cache_create = usage.get("cache_creation_input_tokens") or 0
            row = {
                "provider": provider,
                "model": model,
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
                "cache_read_tokens": int(cache_read),
                "cache_create_tokens": int(cache_create),
                "cost_usd": cost_usd,
                "cost_save_usd": cost_save_usd,
                "via": via,
                "request_id": request_id,
            }
            await client.post(
                f"{SUPABASE_URL}/rest/v1/gateway_usage",
                headers={
                    "apikey": SUPABASE_KEY,
                    "Authorization": f"Bearer {SUPABASE_KEY}",
                    "Content-Profile": "data",
                    "Content-Type": "application/json",
                    "Prefer": "return=minimal",
                },
                json=row,
                timeout=5,
            )
    except Exception as e:
        # never break chat flow over logging
        print(f"⚠️  gateway_usage log failed: {e}")
