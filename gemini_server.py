#!/usr/bin/env python3
# gemini_server.py — Прокси-сервер для Gemini с умной балансировкой и лимитами

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
from contextlib import asynccontextmanager
import traceback

import aiohttp

from _dotenv import load_env

load_env()
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

# Windows: консоль по умолчанию cp866/cp1251 — ломает вывод кириллицы и эмодзи
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PORT = 9322
STATS_FILE = os.path.join(os.path.dirname(__file__), "gemini_stats.json")
WORKSPACE_DIR = os.path.join(os.path.dirname(__file__), "gemini_workspace")

# Create workspace if NOT exists
if not os.path.exists(WORKSPACE_DIR):
    os.makedirs(WORKSPACE_DIR)

MAX_DAILY_CALLS = 500
RPM_LIMIT = 15
RPM_WINDOW = 60
QUOTA_RETRY_INTERVAL = 300  # 5 minutes
BLOCK_RETRY_INTERVAL = 1800  # 30 минут до авто-разблокировки ключа

PAID_KEY = os.getenv("GEMINI_PAID_KEY", "").strip()

# Ключи Google AI Studio задаются через переменную окружения GEMINI_KEYS
GEMINI_KEYS = [
    k.strip()
    for k in os.getenv("GEMINI_KEYS", "").split(",")
    if k.strip()
]

# Ключи можно переопределить через окружение: GEMINI_KEYS="k1,k2,..."
_env_keys = os.getenv("GEMINI_KEYS", "").strip()
if _env_keys:
    GEMINI_KEYS = [k.strip() for k in _env_keys.split(",") if k.strip()]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("gemini_proxy")

# --- ГЛОБАЛЬНОЕ СОСТОЯНИЕ ---
stats = {}
stats_dirty = False
stats_lock = asyncio.Lock()
http_session: Optional[aiohttp.ClientSession] = None

def get_pt_date():
    # Тихоокеанское время (UTC-7 сейчас для летнего времени)
    return (datetime.now(timezone.utc) - timedelta(hours=7)).strftime("%Y-%m-%d")

async def save_stats():
    global stats_dirty
    if not stats_dirty: return
    async with stats_lock:
        try:
            # Не сохраняем временные метки RPM в файл, чтобы не раздувать его
            to_save = {}
            for k, s in stats.items():
                to_save[k] = {ik: iv for ik, iv in s.items() if ik != "request_timestamps"}
            
            with open(STATS_FILE, "w", encoding="utf-8") as f:
                json.dump(to_save, f, indent=2)
            stats_dirty = False
            logger.info("💾 Stats saved to disk.")
        except Exception as e:
            logger.error(f"Ошибка сохранения JSON: {e}")

async def load_stats():
    global stats
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, "r", encoding="utf-8") as f:
                stats = json.load(f)
        except Exception as e:
            logger.error(f"Ошибка загрузки JSON: {e}")
    
    # Инициализация отсутствующих ключей
    pt_today = get_pt_date()
    for k in GEMINI_KEYS:
        if k not in stats:
            stats[k] = {}
        
        # Убеждаемся, что все поля есть
        stats[k].setdefault("total", 0)
        stats[k].setdefault("daily", 0)
        stats[k].setdefault("last_reset", pt_today)
        stats[k].setdefault("blocked", False)
        stats[k].setdefault("blocked_at", 0.0)
        stats[k].setdefault("last_call", 0.0)
        stats[k].setdefault("hit_429", False)
        stats[k].setdefault("last_429", 0.0)
        # Временное поле (не сохраняется)
        stats[k]["request_timestamps"] = []

async def save_loop():
    while True:
        await asyncio.sleep(10)
        await save_stats()

@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_session
    await load_stats()
    http_session = aiohttp.ClientSession()
    save_task = asyncio.create_task(save_loop())
    yield
    # При выключении обязательно сохраняем
    logger.info("🛑 Shutting down server...")
    save_task.cancel()
    await save_stats()
    await http_session.close()
    logger.info("👋 Shutdown complete.")

app = FastAPI(title="Gemini Smart Proxy", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount workspace for static access
app.mount("/workspace", StaticFiles(directory=WORKSPACE_DIR), name="workspace")


NEXT_AVAILABLE = {"ts": 0.0}


async def pick_key(exclude_keys: Optional[List[str]] = None):
    pt_today = get_pt_date()
    now = time.time()
    
    async with stats_lock:
        # Сброс лимитов, если наступил новый день по PT
        for k in GEMINI_KEYS:
            if stats[k].get("last_reset") != pt_today:
                stats[k]["daily"] = 0
                stats[k]["last_reset"] = pt_today
                stats[k]["hit_429"] = False
                stats[k]["last_429"] = 0.0

        candidates = []
        for k in GEMINI_KEYS:
            if exclude_keys and k in exclude_keys: continue
            s = stats[k]
            if s.get("blocked"): continue
            
            # Очистка старых таймстемпов RPM
            s["request_timestamps"] = [t for t in s.get("request_timestamps", []) if now - t < RPM_WINDOW]
            
            # Проверка RPM (15 в минуту)
            if len(s["request_timestamps"]) >= RPM_LIMIT:
                continue
            
            # Проверка 429 (Quotas)
            if s.get("hit_429"):
                # Разрешаем "тестовый" запрос раз в 5 минут
                if now - s.get("last_429", 0) < QUOTA_RETRY_INTERVAL:
                    continue
                # Если же лимит уже израсходован (500), то даже через 5 минут разрешаем только раз в час
                if s["daily"] >= MAX_DAILY_CALLS and (now - s.get("last_429", 0) < 3600):
                    continue
            
            candidates.append(k)

        if not candidates:
            # Все ключи в кулдауне. Считаем, когда освободится хоть один, и ЖДЕМ.
            # Раньше здесь сбрасывались все флаги hit_429, из-за чего прокси
            # сразу перебирал ключи снова и получал 429 пачками.
            soonest = None
            for k in GEMINI_KEYS:
                s = stats[k]
                if s.get("blocked"):
                    # Блокировка могла быть временной (гео-блок Google слетает).
                    # Через BLOCK_RETRY_INTERVAL пробуем ключ заново.
                    if now - s.get("blocked_at", 0) > BLOCK_RETRY_INTERVAL:
                        s["blocked"] = False
                        s["blocked_at"] = 0
                        s["hit_429"] = False
                        s["daily"] = 0
                        logger.info(f"Разблокирован ключ ...{k[-4:]} (прошло {int(BLOCK_RETRY_INTERVAL/60)} мин)")
                    else:
                        continue
                waits = []
                ts = s.get("request_timestamps", [])
                if len(ts) >= RPM_LIMIT and ts:
                    waits.append(ts[0] + RPM_WINDOW - now)
                if s.get("hit_429"):
                    waits.append(s.get("last_429", 0) + QUOTA_RETRY_INTERVAL - now)
                if waits:
                    w = max(0.5, min(waits))
                    soonest = w if soonest is None else min(soonest, w)
            wait = 5.0 if soonest is None else max(1.0, min(float(soonest), 30.0))
            NEXT_AVAILABLE["ts"] = now + wait
            logger.info(f"♻️ No free keys, waiting {wait:.0f}s")
            return None

        # Выбираем ключ с МИНИМАЛЬНЫМ количеством total запросов.
        best_k = min(candidates, key=lambda k: stats[k]["total"])
        stats[best_k]["last_call"] = now
        stats[best_k]["request_timestamps"].append(now)
        return best_k

@app.post("/chat/completions")
@app.post("/v1/chat/completions")
async def proxy_openai(request: Request):
    global stats_dirty
    try:
        payload = await request.json()
    except:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    is_stream = payload.get("stream", False)
    use_paid = request.headers.get("paid", "").lower() == "true"

    # === PAID KEY: прямой запрос без задержек и ротации ===
    if use_paid:
        url = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
        headers = {"Authorization": f"Bearer {PAID_KEY}", "Content-Type": "application/json"}
        try:
            async with http_session.post(url, json=payload, headers=headers, timeout=120) as resp:
                ctype = resp.headers.get("Content-Type", "").lower()
                if "text/event-stream" in ctype or is_stream:
                    content = await resp.read()
                    return StreamingResponse(iter([content]), media_type="text/event-stream")
                resp_json = await resp.json()
                return JSONResponse(status_code=resp.status, content=resp_json)
        except Exception:
            logger.error(f"💥 PAID key error:\n{traceback.format_exc()}")
            return JSONResponse(status_code=502, content={"error": "Paid key request failed"})

    # === FREE KEYS: ротация с задержками ===
    tried_keys = []
    start_time = time.time()
    while True:
        # Если пытаемся уже больше 30 секунд — выходим с ошибкой
        if time.time() - start_time > 30:
            return JSONResponse(status_code=503, content={"error": "All keys failed or busy after 30s retry loop"})

        api_key = await pick_key(exclude_keys=tried_keys)
        if not api_key:
            wait = max(1.0, min(NEXT_AVAILABLE.get("ts", time.time() + 5) - time.time(), 20.0))
            logger.warning(f"♻️ All keys are cooling down, waiting {wait:.0f}s")
            await asyncio.sleep(wait)
            tried_keys = []
            continue

        tried_keys.append(api_key)
        url = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }

        try:
            async with http_session.post(url, json=payload, headers=headers, timeout=60) as resp:
                ctype = resp.headers.get("Content-Type", "").lower()
                
                if resp.status == 200:
                    async with stats_lock:
                        stats[api_key]["total"] += 1
                        stats[api_key]["daily"] += 1
                        stats[api_key]["blocked"] = False
                        stats[api_key]["hit_429"] = False # Сброс 429, если запрос прошел
                        stats_dirty = True
                    
                    if "text/event-stream" in ctype or is_stream:
                        content = await resp.read()
                        return StreamingResponse(iter([content]), media_type="text/event-stream")
                    
                    resp_json = await resp.json()
                    return JSONResponse(resp_json)
                
                # ОБРАБОТКА ОШИБОК
                try:
                    resp_data = await resp.json()
                except:
                    resp_data = {"error": "Non-JSON response", "status": resp.status}

                if resp.status == 429:
                    async with stats_lock:
                        stats[api_key]["hit_429"] = True
                        stats[api_key]["last_429"] = time.time()
                        stats_dirty = True
                    logger.warning(f"⚠️ 429 for key ...{api_key[-4:]}. Switching key...")
                    continue
                
                elif resp.status == 400:
                    err_str = str(resp_data).lower()
                    if "location is not supported" in err_str or "api_key_invalid" in err_str:
                        async with stats_lock:
                            stats[api_key]["blocked"] = True
                            stats[api_key]["blocked_at"] = time.time()
                            stats[api_key]["blocked_at"] = time.time()
                            stats_dirty = True
                        logger.error(f"🚫 Key blocked (...{api_key[-4:]}): {err_str}")
                        continue
                
                if resp.status in [500, 502, 503, 504]:
                    logger.warning(f"⚠️ Google Error {resp.status} for ...{api_key[-4:]}. Switching key...")
                    continue

                # Любая другая ошибка
                return JSONResponse(status_code=resp.status, content=resp_data)
                
        except Exception:
            logger.error(f"💥 Proxy EXC for key ...{api_key[-4:]}:\n{traceback.format_exc()}")
            continue

@app.post("/v1beta/models/{model_name}:generateContent")
@app.post("/v1beta/models/{model_name}:streamGenerateContent")
async def proxy_gemini(model_name: str, request: Request):
    global stats_dirty
    try:
        payload = await request.json()
    except:
        payload = {}

    use_paid = request.headers.get("paid", "").lower() == "true"
    # Определяем, stream это или нет по URL пути
    is_stream = ":streamGenerateContent" in str(request.url)
    alt_param = request.query_params.get("alt", "")

    # === PAID KEY: прямой запрос без задержек и ротации ===
    if use_paid:
        if is_stream:
            action = "streamGenerateContent"
            qs = f"key={PAID_KEY}"
            if alt_param:
                qs += f"&alt={alt_param}"
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:{action}?{qs}"
        else:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={PAID_KEY}"
        try:
            async with http_session.post(url, json=payload, timeout=120) as resp:
                ctype = resp.headers.get("Content-Type", "").lower()
                if is_stream or "text/event-stream" in ctype:
                    content = await resp.read()
                    return StreamingResponse(iter([content]), media_type=resp.headers.get("Content-Type", "text/event-stream"))
                resp_json = await resp.json()
                return JSONResponse(status_code=resp.status, content=resp_json)
        except Exception:
            logger.error(f"💥 PAID key error:\n{traceback.format_exc()}")
            return JSONResponse(status_code=502, content={"error": "Paid key request failed"})

    # === FREE KEYS: ротация с задержками ===
    tried_keys = []
    start_time = time.time()
    
    while True:
        # Если пытаемся уже больше 50 секунд — выходим
        if time.time() - start_time > 50:
            return JSONResponse(status_code=503, content={"error": "Proxy timeout: Google API is unresponsive or all keys hitting 503/429"})

        api_key = await pick_key(exclude_keys=tried_keys)
        if not api_key:
            wait = max(1.0, min(NEXT_AVAILABLE.get("ts", time.time() + 5) - time.time(), 20.0))
            logger.warning(f"♻️ All keys are cooling down, waiting {wait:.0f}s")
            await asyncio.sleep(wait)
            tried_keys = []
            continue

        tried_keys.append(api_key)
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
        
        try:
            # Уменьшаем таймаут до 40с, чтобы успеть перезапустить ключ если зависло
            async with http_session.post(url, json=payload, timeout=40) as resp:
                if resp.status == 200:
                    try:
                        raw_body = await resp.read()
                        if not raw_body: raise ValueError("Empty response body")
                        resp_json = json.loads(raw_body)
                        
                        if "candidates" in resp_json:
                            async with stats_lock:
                                stats[api_key]["total"] += 1
                                stats[api_key]["daily"] += 1
                                stats[api_key]["hit_429"] = False
                                stats_dirty = True
                            return JSONResponse(resp_json)
                        else:
                            # Бывает Google возвращает 200 OK но с ошибкой в JSON
                            logger.warning(f"⚠️ Google 200 with error: {resp_json.get('promptFeedback')}")
                            continue 
                    except Exception:
                        logger.error(f"❌ JSON error key ...{api_key[-4:]}:\n{traceback.format_exc()}")
                        continue
                
                # Ошибки
                try:
                    resp_data = await resp.json()
                except:
                    raw_err = await resp.text()
                    resp_data = {"error": "Non-JSON response", "raw": raw_err[:200]}

                if resp.status == 429:
                    async with stats_lock:
                        stats[api_key]["hit_429"] = True
                        stats[api_key]["last_429"] = time.time()
                        stats_dirty = True
                    logger.warning(f"⚠️ 429 limit hit for ...{api_key[-4:]}. Switching key...")
                    continue
                
                if resp.status in [500, 502, 503, 504]:
                    logger.warning(f"⚠️ Google Error {resp.status} for ...{api_key[-4:]}. Switching key...")
                    # Не блокируем ключ, просто пробуем другой
                    continue

                if resp.status == 400:
                    err_str = str(resp_data).lower()
                    if "location is not supported" in err_str:
                        async with stats_lock:
                            stats[api_key]["blocked"] = True
                            stats_dirty = True
                        logger.error(f"🌍 Geo-blocked key ...{api_key[-4:]}")
                        continue
                    elif "api_key_invalid" in err_str or "api key not valid" in err_str:
                        async with stats_lock:
                            stats[api_key]["blocked"] = True
                            stats_dirty = True
                        logger.error(f"❌ Invalid API key blocked ...{api_key[-4:]}")
                        continue
                    else:
                        logger.error(f"⚠️ Bad Request 400 (Payload error): {resp_data}")
                        return JSONResponse(status_code=400, content=resp_data)
                
                logger.warning(f"⚠️ API error {resp.status} for ...{api_key[-4:]}. Switching key...")
                continue
        except asyncio.TimeoutError:
            logger.warning(f"🕒 Timeout (40s) for key ...{api_key[-4:]}. Trying next...")
            continue
        except Exception:
            logger.error(f"💥 Proxy EXC for key ...{api_key[-4:]}:\n{traceback.format_exc()}")
            continue
    
    return JSONResponse(status_code=500, content={"error": "Infinite retry loop exhausted at 999"})

import subprocess
import sys

# ... (Keep existing code above until health)

@app.post("/execute")
async def execute_code(request: Request):
    try:
        data = await request.json()
        code = data.get("code", "")
        # Поддержка типа: python или shell. По умолчанию python.
        exec_type = data.get("type", "python")
        
        if not code:
            return JSONResponse({"error": "No code provided"}, status_code=400)
            
        logger.info(f"🚀 Executing local {exec_type} block in workspace...")
        
        # Определяем команду запуска
        if exec_type == "shell":
            cmd = ["bash", "-c", code] if os.name != "nt" else ["cmd", "/c", code]
        else:
            cmd = [sys.executable, "-c", code]

        # Запускаем код в рабочей папке
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=WORKSPACE_DIR
        )
        
        stdout, stderr = await process.communicate()
        
        output = stdout.decode().strip()
        error = stderr.decode().strip()
        
        # Список файлов в папке после выполнения (для дебага и отображения)
        files = os.listdir(WORKSPACE_DIR)

        return {
            "output": output,
            "error": error,
            "exit_code": process.returncode,
            "files": files
        }
    except Exception as e:
        logger.error(f"💥 Execution error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/health")

async def health():
    output = []
    async with stats_lock:
        pt_today = get_pt_date()
        for i, k in enumerate(GEMINI_KEYS):
            s = stats.get(k, {})
            # Скрываем ключи, показываем маску
            mask = f"...{k[-4:]}"
            status = "OK"
            if s.get("blocked"): status = "GEO_BLOCKED"
            elif s.get("hit_429"): status = "QUOTA_EXCEEDED"
            
            output.append({
                "index": i + 1,
                "key_mask": mask,
                "total_calls": s.get("total", 0),
                "daily_calls": s.get("daily", 0),
                "status": status,
                "last_call_relative": round(time.time() - s.get("last_call", 0), 1) if s.get("last_call") else None
            })
    
    return {
        "pt_date": pt_today,
        "active_keys": len([k for k in GEMINI_KEYS if not stats[k]['blocked'] and not stats[k]['hit_429']]),
        "total_keys": len(GEMINI_KEYS),
        "keys_stats": output,
        "workspace_files": os.listdir(WORKSPACE_DIR) if os.path.exists(WORKSPACE_DIR) else []
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
