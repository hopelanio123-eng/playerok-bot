# -*- coding: utf-8 -*-
"""
ai_providers.py — единый слой над провайдерами ИИ.

Раньше бот ходил только в Gemini (через локальный прокси :9322). Когда у
бесплатных ключей Google выбирается дневная квота (429) - анализ картинок
падал и бот молчал.

Теперь есть цепочка провайдеров с автоматическим переключением:

  1. gemini     - локальный прокси gemini_server.py (:9322).
                  Бесплатная квота Google, работает пока не выбрана.
  2. openrouter - бесплатные модели OpenRouter (нужен бесплатный ключ,
                  без карты: https://openrouter.ai/keys).
                  Есть модели с картинкой: nvidia/nemotron-3.5-super-vl:free
  3. nvidia     - NVIDIA NIM (build.nvidia.com), бесплатные кредиты.
  4. cloudflare - Cloudflare Workers AI, 10 000 нейронок в сутки бесплатно.
  5. pollinations - Pollinations Gen API (enter.pollinations.ai/key).
  6. groq       - Groq. Ключи уже есть в проекте, но только текстовые
                  модели (vision у Groq убрали) - годится для торга.

Настройка (переменные окружения или .env):
  AI_PROVIDERS=gemini,openrouter,nvidia,cloudflare,pollinations,groq
  OPENROUTER_API_KEY=sk-or-v1-...
  NVIDIA_API_KEY=nvapi-...
  CLOUDFLARE_ACCOUNT_ID=...
  CLOUDFLARE_API_TOKEN=...
  POLLINATIONS_API_KEY=...
  GROQ_API_KEYS=gsk_...,gsk_...
  GROQ_TEXT_MODEL=openai/gpt-oss-120b
"""

import base64
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

import httpx

from _dotenv import load_env

load_env()

logger = logging.getLogger("ai_providers")

# Ключи Groq задаются через переменную окружения GROQ_API_KEYS
GROQ_KEYS_DEFAULT: List[str] = []

GEMINI_SERVER = os.getenv("GEMINI_SERVER", "http://127.0.0.1:9322")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite-preview")

OPENROUTER_URL = os.getenv("OPENROUTER_URL", "https://openrouter.ai/api/v1/chat/completions")
OPENROUTER_VISION_MODEL = os.getenv(
    "OPENROUTER_VISION_MODEL", "nvidia/nemotron-3.5-super-vl:free"
)
OPENROUTER_TEXT_MODEL = os.getenv(
    "OPENROUTER_TEXT_MODEL", "thinkingmachines/inkling-small:free"
)

# NVIDIA NIM (build.nvidia.com) - бесплатные кредиты, есть vision
NVIDIA_URL = os.getenv("NVIDIA_URL", "https://integrate.api.nvidia.com/v1/chat/completions")
NVIDIA_VISION_MODEL = os.getenv(
    "NVIDIA_VISION_MODEL", "nvidia/nemotron-3.5-super-vl-120b-a12b"
)
NVIDIA_TEXT_MODEL = os.getenv("NVIDIA_TEXT_MODEL", "nvidia/nemotron-3.5-lightning-30b-a3b")

# Cloudflare Workers AI - 10000 нейронок в сутки бесплатно, есть vision
CLOUDFLARE_URL = os.getenv("CLOUDFLARE_URL", "")
CLOUDFLARE_VISION_MODEL = os.getenv(
    "CLOUDFLARE_VISION_MODEL", "@cf/meta/llama-3.2-11b-vision-instruct"
)
CLOUDFLARE_TEXT_MODEL = os.getenv("CLOUDFLARE_TEXT_MODEL", "@cf/meta/llama-3.1-8b-instruct")

# Pollinations (Gen API) - ключ на enter.pollinations.ai/key
POLLINATIONS_URL = os.getenv("POLLINATIONS_URL", "https://gen.pollinations.ai/v1/chat/completions")
POLLINATIONS_VISION_MODEL = os.getenv("POLLINATIONS_VISION_MODEL", "openai/gpt-4o-mini")
POLLINATIONS_TEXT_MODEL = os.getenv("POLLINATIONS_TEXT_MODEL", "openai/gpt-4o-mini")

GROQ_URL = os.getenv("GROQ_URL", "https://api.groq.com/openai/v1/chat/completions")
GROQ_TEXT_MODEL = os.getenv("GROQ_TEXT_MODEL", "openai/gpt-oss-120b")


def _split(value: str) -> List[str]:
    return [x.strip() for x in value.replace(";", ",").split(",") if x.strip()]


ORDER_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_order.json")
VALID = ("gemini", "openrouter", "nvidia", "cloudflare", "pollinations", "groq")
DEFAULT_ORDER = "gemini,openrouter,nvidia,cloudflare,pollinations,groq"


def provider_order() -> List[str]:
    """Порядок провайдеров: файл ai_order.json -> переменная окружения -> дефолт."""
    order: List[str] = []
    try:
        if os.path.exists(ORDER_FILE):
            with open(ORDER_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            order = [p for p in (data.get("order") or []) if p in VALID]
    except Exception:
        order = []
    if not order:
        raw = os.getenv("AI_PROVIDERS", DEFAULT_ORDER)
        order = [p for p in _split(raw) if p in VALID]
    return order or ["gemini"]


def save_order(order: List[str]) -> List[str]:
    """Сохраняет порядок провайдеров в ai_order.json."""
    clean = [p for p in order if p in VALID]
    # убираем дубли, сохраняя порядок
    unique: List[str] = []
    for p in clean:
        if p not in unique:
            unique.append(p)
    try:
        with open(ORDER_FILE, "w", encoding="utf-8") as fh:
            json.dump({"order": unique}, fh, ensure_ascii=False, indent=2)
    except Exception as exc:
        logger.warning(f"Не удалось сохранить порядок провайдеров: {exc}")
    return unique


def openrouter_key() -> str:
    return os.getenv("OPENROUTER_API_KEY", "").strip()


def nvidia_key() -> str:
    return os.getenv("NVIDIA_API_KEY", "").strip()


def cloudflare_creds() -> tuple:
    """Возвращает (account_id, token) для Cloudflare Workers AI."""
    acc = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
    token = os.getenv("CLOUDFLARE_API_TOKEN", "").strip()
    return acc, token


def pollinations_key() -> str:
    return os.getenv("POLLINATIONS_API_KEY", "").strip()


def groq_keys() -> List[str]:
    raw = os.getenv("GROQ_API_KEYS", "").strip()
    if raw:
        return _split(raw)
    return list(GROQ_KEYS_DEFAULT)


def schema_to_text(schema: Dict[str, Any]) -> str:
    """Описывает JSON-схему словами - для провайдеров без response_schema."""
    props = schema.get("properties", {})
    lines = []
    for name, spec in props.items():
        desc = spec.get("description", "")
        t = spec.get("type", "string")
        if t == "array":
            lines.append(f'  "{name}": array of strings - {desc}')
        elif t == "boolean":
            lines.append(f'  "{name}": true/false - {desc}')
        else:
            lines.append(f'  "{name}": string - {desc}')
    required = schema.get("required", [])
    return "\n".join(lines) + "\nRequired keys: " + ", ".join(required)


def extract_json(text: str) -> Optional[dict]:
    """Достаёт JSON из ответа модели (терпимо к ```json ... ``` и болтовне)."""
    if not text:
        return None
    raw = text.strip()
    if "```" in raw:
        raw = re.sub(r"```[a-zA-Z]*", "", raw).replace("```", "").strip()
    start = raw.find("{")
    end = raw.rfind("}") + 1  # +1 - иначе закрывающая скобка отрезается
    if start == -1 or end <= 0:
        return None
    try:
        data = json.loads(raw[start:end])
        return data if isinstance(data, dict) else None
    except Exception:
        return None


# ===========================================================================
# Провайдеры
# ===========================================================================


async def _gemini(
    system_prompt: str,
    prompt: str,
    images: List[dict],
    schema: Dict[str, Any],
    history: Optional[List[dict]] = None,
) -> Optional[dict]:
    """Локальный прокси Gemini (gemini_server.py)."""
    parts: List[dict] = [{"text": prompt}]
    for img in images:
        parts.append({"inline_data": img})

    if history:
        contents = []
        for msg in history:
            role = "model" if msg.get("role") == "assistant" else "user"
            contents.append({"role": role, "parts": [{"text": msg.get("content", "")}]})
        contents.append({"role": "user", "parts": [{"text": prompt}]})
    else:
        contents = [{"role": "user", "parts": parts}]

    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": contents,
        "generationConfig": {
            "response_mime_type": "application/json",
            "response_schema": schema,
        },
    }

    url = f"{GEMINI_SERVER}/v1beta/models/{GEMINI_MODEL}:generateContent"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=10.0)) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code != 200:
                logger.warning(f"[gemini] HTTP {resp.status_code}: {resp.text[:120]}")
                return None
            data = resp.json()
    except Exception as exc:
        logger.warning(f"[gemini] connection error: {exc}")
        return None

    candidates = data.get("candidates") or [{}]
    for part in (candidates[0].get("content", {}).get("parts", []) or []):
        if part.get("thought"):
            continue
        text = part.get("text")
        if not text:
            continue
        parsed = extract_json(text)
        if parsed:
            return parsed
    logger.warning("[gemini] no JSON in response")
    return None


def _openai_messages(system_prompt: str, prompt: str, images: List[dict], history) -> List[dict]:
    content: List[dict] = []
    if history:
        for msg in history:
            content.append({"role": msg.get("role", "user"), "content": msg.get("content", "")})
    text = prompt
    if images:
        content.append(
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": text},
                    *[
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{img.get('mime_type', 'image/jpeg')};base64,{img.get('data', '')}"
                            },
                        }
                        for img in images
                    ],
                ],
            }
        )
    else:
        content.append({"role": "user", "content": text})
    return [{"role": "system", "content": system_prompt}, *content]


async def _openrouter(
    system_prompt: str,
    prompt: str,
    images: List[dict],
    schema: Dict[str, Any],
    history: Optional[List[dict]] = None,
) -> Optional[dict]:
    key = openrouter_key()
    if not key:
        return None

    model = OPENROUTER_VISION_MODEL if images else OPENROUTER_TEXT_MODEL
    payload = {
        "model": model,
        "messages": _openai_messages(system_prompt, prompt, images, history),
        "max_tokens": 2000,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "HTTP-Referer": "http://localhost",
        "X-Title": "tg-bot",
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=10.0)) as client:
            resp = await client.post(OPENROUTER_URL, json=payload, headers=headers)
            if resp.status_code != 200:
                logger.warning(f"[openrouter] HTTP {resp.status_code}: {resp.text[:150]}")
                return None
            data = resp.json()
    except Exception as exc:
        logger.warning(f"[openrouter] connection error: {exc}")
        return None

    try:
        text = data["choices"][0]["message"]["content"]
    except Exception:
        logger.warning(f"[openrouter] unexpected response: {str(data)[:150]}")
        return None
    return extract_json(text)


async def _groq(
    system_prompt: str,
    prompt: str,
    images: List[dict],
    schema: Dict[str, Any],
    history: Optional[List[dict]] = None,
) -> Optional[dict]:
    # У Groq остались только текстовые модели
    if images:
        return None

    payload = {
        "model": GROQ_TEXT_MODEL,
        "messages": _openai_messages(system_prompt, prompt, [], history),
        "max_tokens": 2000,
        "temperature": 0.3,
        "response_format": {"type": "json_object"},
    }

    for key in groq_keys():
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
                resp = await client.post(
                    GROQ_URL,
                    json=payload,
                    headers={"Authorization": f"Bearer {key}"},
                )
                if resp.status_code != 200:
                    logger.warning(f"[groq] HTTP {resp.status_code}: {resp.text[:160]}")
                    continue
                data = resp.json()
                text = data["choices"][0]["message"]["content"]
                parsed = extract_json(text)
                if parsed:
                    return parsed
        except Exception as exc:
            logger.warning(f"[groq] ошибка: {exc}")
            continue
    logger.warning("[groq] ни один ключ не дал валидный json")
    return None


async def _nvidia(
    system_prompt: str,
    prompt: str,
    images: List[dict],
    schema: Dict[str, Any],
    history: Optional[List[dict]] = None,
) -> Optional[dict]:
    key = nvidia_key()
    if not key:
        return None

    model = NVIDIA_VISION_MODEL if images else NVIDIA_TEXT_MODEL
    payload = {
        "model": model,
        "messages": _openai_messages(system_prompt, prompt, images, history),
        "max_tokens": 2000,
        "temperature": 0.2,
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=10.0)) as client:
            resp = await client.post(
                NVIDIA_URL, json=payload, headers={"Authorization": f"Bearer {key}"}
            )
            if resp.status_code != 200:
                logger.warning(f"[nvidia] HTTP {resp.status_code}: {resp.text[:150]}")
                return None
            data = resp.json()
    except Exception as exc:
        logger.warning(f"[nvidia] connection error: {exc}")
        return None

    try:
        text = data["choices"][0]["message"]["content"]
    except Exception:
        logger.warning(f"[nvidia] unexpected response: {str(data)[:150]}")
        return None
    return extract_json(text)


async def _cloudflare(
    system_prompt: str,
    prompt: str,
    images: List[dict],
    schema: Dict[str, Any],
    history: Optional[List[dict]] = None,
) -> Optional[dict]:
    account, token = cloudflare_creds()
    if not account or not token:
        return None

    url = CLOUDFLARE_URL or (
        f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/v1/chat/completions"
    )
    model = CLOUDFLARE_VISION_MODEL if images else CLOUDFLARE_TEXT_MODEL
    payload = {
        "model": model,
        "messages": _openai_messages(system_prompt, prompt, images, history),
        "max_tokens": 2000,
        "temperature": 0.2,
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=10.0)) as client:
            resp = await client.post(
                url, json=payload, headers={"Authorization": f"Bearer {token}"}
            )
            if resp.status_code != 200:
                logger.warning(f"[cloudflare] HTTP {resp.status_code}: {resp.text[:150]}")
                return None
            data = resp.json()
    except Exception as exc:
        logger.warning(f"[cloudflare] connection error: {exc}")
        return None

    try:
        text = data["choices"][0]["message"]["content"]
    except Exception:
        logger.warning(f"[cloudflare] unexpected response: {str(data)[:150]}")
        return None
    return extract_json(text)


async def _pollinations(
    system_prompt: str,
    prompt: str,
    images: List[dict],
    schema: Dict[str, Any],
    history: Optional[List[dict]] = None,
) -> Optional[dict]:
    key = pollinations_key()
    if not key:
        return None

    model = POLLINATIONS_VISION_MODEL if images else POLLINATIONS_TEXT_MODEL
    payload = {
        "model": model,
        "messages": _openai_messages(system_prompt, prompt, images, history),
        "max_tokens": 2000,
        "temperature": 0.2,
    }
    headers = {"Authorization": f"Bearer {key}"}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=10.0)) as client:
            resp = await client.post(POLLINATIONS_URL, json=payload, headers=headers)
            if resp.status_code != 200:
                logger.warning(f"[pollinations] HTTP {resp.status_code}: {resp.text[:150]}")
                return None
            data = resp.json()
    except Exception as exc:
        logger.warning(f"[pollinations] connection error: {exc}")
        return None

    try:
        text = data["choices"][0]["message"]["content"]
    except Exception:
        logger.warning(f"[pollinations] unexpected response: {str(data)[:150]}")
        return None
    return extract_json(text)


PROVIDERS = {
    "gemini": _gemini,
    "openrouter": _openrouter,
    "nvidia": _nvidia,
    "cloudflare": _cloudflare,
    "pollinations": _pollinations,
    "groq": _groq,
}

PROVIDER_TITLES = {
    "gemini": "Gemini (прокси :9322)",
    "openrouter": "OpenRouter (бесплатные модели)",
    "nvidia": "NVIDIA NIM (бесплатные кредиты)",
    "cloudflare": "Cloudflare Workers AI (10k/сутки)",
    "pollinations": "Pollinations (Gen API)",
    "groq": "Groq (только текст)",
}


# ===========================================================================
# Публичный слой
# ===========================================================================


def _add_schema_hint(prompt: str, schema: Dict[str, Any]) -> str:
    # ВАЖНО: Groq при response_format=json_object требует слово "json"
    # (именно в нижнем регистре) в тексте запроса, иначе 400.
    return (
        prompt
        + "\n\nОтветь ТОЛЬКО валидным json, без markdown и пояснений, строго по схеме:\n"
        + schema_to_text(schema)
    )


async def call_ai(
    system_prompt: str,
    prompt: str,
    schema: Dict[str, Any],
    images: Optional[List[dict]] = None,
    history: Optional[List[dict]] = None,
    kind: str = "vision",
) -> Tuple[Optional[dict], str]:
    """Пробует провайдеров по очереди. Возвращает (json, имя провайдера)."""
    images = images or []
    full_prompt = _add_schema_hint(prompt, schema)

    for name in provider_order():
        func = PROVIDERS.get(name)
        if not func:
            continue
        try:
            result = await func(system_prompt, full_prompt, images, schema, history)
        except Exception as exc:
            logger.warning(f"[{name}] исключение: {exc}")
            continue
        if result:
            logger.info(f"✅ [AI:{name}] ответ получен")
            return result, name
        logger.info(f"[{name}] не дал результат, пробуем следующий")

    return None, ""


async def provider_status() -> List[dict]:
    """Проверяет доступность провайдеров (для /ai)."""
    out = []

    for name in ("gemini", "openrouter", "nvidia", "cloudflare", "pollinations", "groq"):
        ok = False
        detail = ""
        if name == "gemini":
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    r = await client.get(f"{GEMINI_SERVER}/health")
                ok = r.status_code == 200
                detail = f"модель {GEMINI_MODEL}"
            except Exception as exc:
                detail = f"прокси недоступен ({type(exc).__name__})"
        elif name == "openrouter":
            key = openrouter_key()
            ok = bool(key)
            detail = (
                f"ключ есть, vision {OPENROUTER_VISION_MODEL}"
                if key
                else "нужен ключ: openrouter.ai/keys (без карты)"
            )
        elif name == "nvidia":
            key = nvidia_key()
            ok = bool(key)
            detail = (
                f"ключ есть, vision {NVIDIA_VISION_MODEL}"
                if key
                else "нужен ключ: build.nvidia.com (бесплатные кредиты)"
            )
        elif name == "cloudflare":
            account, token = cloudflare_creds()
            ok = bool(account and token)
            detail = (
                f"account {account[:6]}..., vision {CLOUDFLARE_VISION_MODEL}"
                if ok
                else "нужны CLOUDFLARE_ACCOUNT_ID и CLOUDFLARE_API_TOKEN (бесплатно)"
            )
        elif name == "pollinations":
            key = pollinations_key()
            ok = bool(key)
            detail = (
                f"ключ есть, vision {POLLINATIONS_VISION_MODEL}"
                if key
                else "нужен ключ: enter.pollinations.ai/key (бесплатно)"
            )
        elif name == "groq":
            keys = groq_keys()
            detail = f"ключей: {len(keys)}, модель {GROQ_TEXT_MODEL} (только текст)"

        out.append(
            {
                "name": name,
                "title": PROVIDER_TITLES.get(name, name),
                "available": ok,
                "detail": detail,
                "priority": provider_order().index(name) + 1
                if name in provider_order()
                else 99,
            }
        )
    return out
