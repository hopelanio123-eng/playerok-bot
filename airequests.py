import aiohttp
import asyncio
import json
import logging
import base64
from typing import List, Optional

import ai_providers

logger = logging.getLogger("gemini_client")

# Прокси Gemini (остался как первый провайдер в цепочке)
GEMINI_SERVER = ai_providers.GEMINI_SERVER
GEMINI_MODEL = ai_providers.GEMINI_MODEL

GEMINI_SCHEMA = {
    "type": "object",
    "properties": {
        "error":            {"type": "boolean",  "description": "True only if NO collection name can be identified from the title or image."},
        "collection":       {"type": "string",   "description": "The identified collection name (e.g., 'Plush Pepe', 'Grey Set'). Use 'none' if unidentified."},
        "model":            {"type": "string",   "description": "Comma-separated list of visual model names labels seen on gifts. Use 'none' if no labels are visible."},
        "backdrop":         {"type": "string",   "description": "Comma-separated list of backdrop/attributes names seen on gifts. Use 'none' if no labels are visible."},
        "ids":              {"type": "string",   "description": "Comma-separated list of numeric IDs found after '#' (e.g., '1479, 1480'). Use 'none' if not found."},
        "multi_gift":       {"type": "boolean",  "description": "True if there are 2 or more gifts clearly visible in the image."},
    },
    "required": ["error", "collection", "model", "backdrop", "ids", "multi_gift"],
}

BARGAIN_SCHEMA = {
    "type": "object",
    "properties": {
        "ended": {
            "type": "boolean",
            "description": "Флаг завершения торга. True, если продавец согласился на цену, либо если окончательно отказал/послал. Иначе False (торг продолжается)."
        },
        "messages": {
            "type": "array",
            "description": "Массив сообщений для отправки продавцу. Можно разбить свою мысль на 2-3 коротких сообщения (имитация печати).",
            "items": {
                "type": "object",
                "properties": {
                    "delay": {"type": "integer", "description": "Задержка перед отправкой сообщения в секундах (например, от 1 до 5)"},
                    "message": {"type": "string", "description": "Текст сообщения по заданным инструкциям."}
                },
                "required": ["delay", "message"]
            }
        },
        "end_reason": {
            "type": "string",
            "description": "Краткая причина завершения торга (если ended = true). Например: 'продавец согласился скинуть цену' или 'продавец отказался торговаться'."
        }
    },
    "required": ["ended", "messages"]
}

SYSTEM_INSTRUCTION = (
    "You are a professional image and text analyzer for Telegram gifts and NFTs.\n\n"
    "CRITICAL RULES:\n"
    "1) ALL output in ENGLISH only.\n"
    "2) NO APOSTROPHES (Valentines, Xmas).\n"
    "3) FRAGMENT IDS: Extract numeric IDs found after '#' symbol. IMPORTANT: In some cases IDs are written with commas (e.g. '#130,170'). Commas are thousands separators and NOT a separator for a different ID. Remove commas and merge decimals to get the full ID. Example: '#130,170' -> '130170'. If you see '#' in different places, then they are different IDs.\n"
    "4) TEXT LABELS: Extract clearly legible labels. If a label (like 'Witch Hat', 'Froggy') looks like the main item name, it is likely the COLLECTION name.\n"
    "5) MULTI-ITEM: If there are 2 or more DIFFERENT gifts, use lists.\n"
    "6) MASSIVE LOTS: If there are MORE THAN 10 gifts, DO NOT extract individual models, backdrops, or ids! Just return the collection name(s) and set model, backdrop, and ids to 'none'.\n"
    "7) NO DUPLICATION: If ONLY ONE gift, do not repeat labels.\n"
    "8) NO HALLUCINATION: If determined, use 'none'."
)


async def download_image_base64(session: aiohttp.ClientSession, url: str) -> Optional[dict]:
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 200:
                data = await resp.read()
                return {"mime_type": "image/jpeg", "data": base64.b64encode(data).decode()}
    except Exception:
        pass
    return None


def _build_vision_prompt(title: str, long_desc: str, has_images: bool) -> str:
    prompt = f"Lot title for context: '{title}'\n"
    if long_desc:
        prompt += f"Seller's detailed description: '{long_desc}'\n"

    if has_images:
        prompt += (
            "\nAnalyze the screenshot(s) and the Seller's detailed description (if provided). "
            "For each gift, extract ALL explicit text labels for collection, model, backdrop. "
            "IMPORTANT: The seller's description might contain unrelated garbage or spam. ONLY use data from the description if it clearly relates to Telegram NFT gift attributes (names, models, backgrounds). Ignore any irrelevant information.\n"
            "Return unique names as comma-separated values. If multiple gifts seen, set multi_gift=true.\n"
            "IMPORTANT: Extract ALL numeric IDs found. Commas in IDs are thousands separators (e.g. '#130,170' -> '130170'). If there are multiple separate IDs, they will start with different '#' symbols.\n"
            "English, no apostrophes."
        )
    else:
        prompt += (
            "\nAnalyze ONLY the Title and Seller's detailed description (there is NO screenshot available). "
            "For each gift, extract ALL explicit text labels for collection, model, backdrop. "
            "IMPORTANT: The seller's description might contain unrelated garbage or spam. ONLY use data from the description if it clearly relates to Telegram NFT gift attributes (names, models, backgrounds). Ignore any irrelevant information.\n"
            "Do NOT guess visual attributes. Only extract what is clearly written in text.\n"
            "Return unique names as comma-separated values. If multiple gifts seen, set multi_gift=true.\n"
            "IMPORTANT: Extract ALL numeric IDs found. Commas in IDs are thousands separators (e.g. '#130,170' -> '130170'). If there are multiple separate IDs, they will start with different '#' symbols.\n"
            "English, no apostrophes."
        )
    return prompt


async def call_gemini_server(image_urls: List[str], title: str, long_desc: str = "") -> Optional[dict]:
    """Анализ картинки подарка.

    Пробует провайдеров по очереди (Gemini -> OpenRouter -> Ollama),
    чтобы не зависеть от одной бесплатной квоты.
    """
    has_images = len(image_urls) > 0
    image_parts: List[dict] = []
    if has_images:
        async with aiohttp.ClientSession() as session:
            downloads = await asyncio.gather(
                *[download_image_base64(session, u) for u in image_urls[:2]]
            )
        image_parts = [d for d in downloads if d]

    prompt = _build_vision_prompt(title, long_desc, bool(image_parts))

    res, provider = await ai_providers.call_ai(
        SYSTEM_INSTRUCTION, prompt, GEMINI_SCHEMA, images=image_parts, kind="vision"
    )
    if not res:
        return None

    if "collection" not in res and "error" not in res:
        return None

    c, m, gid = res.get("collection"), res.get("model"), res.get("ids")
    logger.info(f"✅ [AI:{provider}] C={c} | M={m} | ID=#{gid}")
    return res


async def call_gemini_bargain(system_prompt: str, user_prompt: str, history: list) -> Optional[dict]:
    """Торг: ответ модели в JSON по схеме BARGAIN_SCHEMA."""
    contents = [{"role": "user", "content": user_prompt}]

    if history:
        for msg in history:
            if msg["author"] == "system":
                continue
            role = "assistant" if msg["author"] == "me" else "user"
            content_text = msg["text"] or "[изображение/стикер]"
            date_str = f"[{msg.get('date', 'неизвестно')}] " if msg.get("date") else ""
            text_with_date = f"{date_str}{content_text}"

            if contents and contents[-1]["role"] == role:
                contents[-1]["content"] += f"\n\n{text_with_date}"
            else:
                contents.append({"role": role, "content": text_with_date})

    if contents[-1]["role"] == "user":
        contents.append({"role": "user", "content": "[напиши ответ продавцу]"})

    res, provider = await ai_providers.call_ai(
        system_prompt, user_prompt, BARGAIN_SCHEMA, history=contents, kind="chat"
    )
    if not res:
        return None

    return {"json": res, "thought": "", "raw": {"provider": provider}}
