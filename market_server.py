import asyncio
import json
import logging
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

from _dotenv import load_env

load_env()
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# Windows: консоль по умолчанию cp866/cp1251 — ломает вывод кириллицы и эмодзи
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# =============================================================================
# --- КОНФИГУРАЦИЯ ---
# =============================================================================
PORT = 9321

# Токены задаются через переменную окружения GIFT_SATELLITE_TOKENS
API_TOKENS = [
    t.strip()
    for t in os.getenv("GIFT_SATELLITE_TOKENS", "").split(",")
    if t.strip()
]
# Переопределение из окружения: GIFT_SATELLITE_TOKENS="Token xxx,Token yyy"
_env_tokens = os.getenv("GIFT_SATELLITE_TOKENS", "").strip()
if _env_tokens:
    API_TOKENS = [t.strip() for t in _env_tokens.split(",") if t.strip()]
API_BASE_URL = os.getenv('GIFT_SATELLITE_API_URL', 'https://gift-satellite.dev/api').rstrip('/')

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("market_server")

app = FastAPI(title="Minimalist Satellite Market Server", version="3.0.0")

# =============================================================================
# --- RATE LIMITER (подход из gstatesearch.py) ---
# =============================================================================
class TokenRateLimiter:
    """Обеспечивает соблюдение лимитов для каждого токена."""
    def __init__(self, tokens: List[str]) -> None:
        self._tokens = tokens
        self._n = len(tokens)
        self._last_call: List[Dict[str, float]] = [{} for _ in tokens]
        self._locks: List[Dict[str, asyncio.Lock]] = [{} for _ in tokens]
        self._inflight: List[int] = [0] * len(tokens)
        self._cursor = 0

    def _endpoint_key(self, method: str, url: str) -> Tuple[Optional[str], float]:
        path = url.replace(API_BASE_URL, "")
        search_match = re.match(r"/search/([^/]+)/", path)
        if search_match:
            market = search_match.group(1)
            # Лимиты: mrkt - 2.1с, остальные (portals) - 2.2с
            interval = 2.1 if market == "mrkt" else 2.2
            return f"{method.upper()} /search/{market}", interval
        if path.startswith("/history/"):
            return f"{method.upper()} /history", 2.1
        if path.startswith("/gift/collections"):
            return "GET /collections", 0.5
        return None, 0.0

    def _pick_slot(self, method: str, url: str) -> int:
        now = time.monotonic()
        best_slot = self._cursor % self._n
        best_score: Optional[float] = None

        for offset in range(self._n):
            slot = (self._cursor + offset) % self._n
            key, interval = self._endpoint_key(method, url)
            if key and interval > 0:
                elapsed = now - self._last_call[slot].get(key, 0.0)
                wait = max(0.0, interval - elapsed)
            else:
                wait = 0.0
            # Учитываем текущую нагрузку на токен
            score = wait + self._inflight[slot] * 0.40
            if best_score is None or score < best_score:
                best_slot = slot
                best_score = score

        self._cursor = (best_slot + 1) % self._n
        return best_slot

    async def acquire(self, method: str, url: str) -> Tuple[str, int]:
        slot = self._pick_slot(method, url)
        self._inflight[slot] += 1
        key, interval = self._endpoint_key(method, url)
        if key and interval > 0:
            lock = self._locks[slot].setdefault(key, asyncio.Lock())
            async with lock:
                now = time.monotonic()
                elapsed = now - self._last_call[slot].get(key, 0.0)
                wait_for = interval - elapsed
                if wait_for > 0:
                    await asyncio.sleep(wait_for)
                self._last_call[slot][key] = time.monotonic()
        return self._tokens[slot], slot

    def release(self, slot: int) -> None:
        if 0 <= slot < self._n:
            self._inflight[slot] = max(0, self._inflight[slot] - 1)

token_limiter = TokenRateLimiter(API_TOKENS)
http_client = httpx.AsyncClient(timeout=30.0, headers={"User-Agent": "SatelliteMarketServer/3.0"})

# =============================================================================
# --- АСИНХРОННЫЕ ЗАПРОСЫ (из gstatesearch.py) ---
# =============================================================================
async def _fetch_url(url: str, method: str = "GET", payload: Optional[dict] = None, params: Optional[dict] = None) -> Optional[Any]:
    for attempt in range(3):
        token, slot = await token_limiter.acquire(method, url)
        headers = {"Authorization": token}
        try:
            resp = await http_client.request(method, url, headers=headers, json=payload, params=params)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                logger.warning(f"[API] 429 Rate Limit on slot {slot+1}, retrying...")
                await asyncio.sleep(2.0 + attempt * 2)
                continue
            if resp.status_code == 400:
                # Неверный формат коллекции / неизвестное имя — это не критично
                logger.warning(f"[API] 400 Bad Request for {url}")
                return None
            logger.error(f"[API] Error {resp.status_code} for {url}")
            return None
        except Exception as e:
            logger.error(f"[API] Connection error for {url}: {e}")
            await asyncio.sleep(1.0)
        finally:
            token_limiter.release(slot)
    return None

# =============================================================================
# --- ЛОГИКА (из logic.py) ---
# =============================================================================
async def get_market_data(collection: str, model: str = "", backdrop: str = "", target_market: str = ""):
    """Агрегирует данные поиска и истории через Satellite API."""
    c_enc = urllib.parse.quote(collection)
    params = {}
    if model: params["models"] = model
    if backdrop: params["backdrops"] = backdrop
    
    # Поиск по маркетам
    markets = [target_market] if target_market in ["mrkt", "portals"] else ["mrkt", "portals"]
    search_tasks = [
        _fetch_url(f"{API_BASE_URL}/search/{m}/{c_enc}", params=params) for m in markets
    ]
    
    # История (Activity)
    history_payload = {
        "models": [model] if model else [],
        "backdrops": [backdrop] if backdrop else [],
        "sortBy": "date",
        "page": 0,
        "pageSize": 20
    }
    # Фильтруем историю по маркету, если он указан
    if target_market in ["mrkt", "portals"]:
        history_payload["markets"] = target_market
        
    history_task = _fetch_url(f"{API_BASE_URL}/history/{c_enc}", "POST", history_payload)
    
    results = await asyncio.gather(*search_tasks, history_task)
    
    on_sale = []
    for i in range(len(search_tasks)):
        res = results[i]
        market_name = markets[i] if i < len(markets) else "unknown"
        if res and isinstance(res, list):
            for item in res:
                price = item.get("normalizedPrice")
                if price is None and "value" in item:
                    try:
                        price = float(item["value"].split()[0])
                    except:
                        price = 0.0
                on_sale.append({
                    "price": price or 0.0,
                    "market": market_name,
                    "tg_id": item.get("slug", ""),
                    "raw": item
                })
    
    on_sale.sort(key=lambda x: float(x.get("price", 999999)))
    
    activity = []
    history_res = results[-1]
    if history_res and isinstance(history_res, dict):
        for item in history_res.get("content", []):
            try:
                price = float(item.get("normalizedPrice", 0))
            except:
                price = 0.0
            activity.append({
                "price": price,
                "market": item.get("market", target_market if target_market in ["mrkt", "portals"] else "?"),
                "tg_id": item.get("id") or item.get("slug", ""),
                "sold_at": item.get("soldAt", ""),
                "raw": item
            })
        
    return {
        "query": {"collection": collection, "model": model, "backdrop": backdrop},
        "on_sale": on_sale,
        "on_sale_count": len(on_sale),
        "activity": activity
    }

def get_slug(name: str) -> str:
    return re.sub(r'[^a-zA-Z0-9]', '', name).lower()

async def get_collection_models(collection: str) -> List[str]:
    """Запрашивает список моделей коллекции через Satellite API."""
    c_enc = urllib.parse.quote(collection)
    data = await _fetch_url(f"{API_BASE_URL}/gift/models/{c_enc}")
    if data and isinstance(data, list):
        return [m["name"] for m in data if "name" in m]
    return []

# =============================================================================
# --- FASTAPI ENDPOINTS ---
# =============================================================================
class SearchRequest(BaseModel):
    collection: str
    model: str = ""
    backdrop: str = ""
    target_market: str = ""

class GiftInfoRequest(BaseModel):
    collection: str
    gift_id: str

@app.post("/search_all")
@app.post("/price")
async def api_search(req: SearchRequest):
    if not req.collection:
        raise HTTPException(400, "Collection is required")
    data = await get_market_data(req.collection, req.model, req.backdrop, req.target_market)
    return data

@app.post("/gift_info")
async def api_gift_info(req: GiftInfoRequest):
    """Возвращает точные атрибуты предмета через Satellite API."""
    slug = f"{get_slug(req.collection)}-{req.gift_id.replace('#', '')}"
    data = await _fetch_url(f"{API_BASE_URL}/gift/by-slug/{slug}")
    if data and isinstance(data, dict):
        return {
            "model": data.get("modelName", ""),
            "backdrop": data.get("backdropName", ""),
            "collection": data.get("collectionName", req.collection)
        }
    return {"error": "Gift not found"}

@app.post("/validate_metadata")
async def api_validate_metadata(req: SearchRequest):
    """Валидирует и исправляет названия на основе данных Satellite API."""
    models = await get_collection_models(req.collection)
    import difflib
    
    res_model = req.model
    if models and req.model:
        matches = difflib.get_close_matches(req.model, models, n=1, cutoff=0.6)
        if matches:
            res_model = matches[0]
            
    return {
        "collection": req.collection,
        "model": res_model,
        "backdrop": req.backdrop,
        "exists": len(models) > 0,
        "slug": get_slug(req.collection)
    }

@app.get("/collections")
async def get_collections():
    """Список всех коллекций."""
    data = await _fetch_url(f"{API_BASE_URL}/gift/collections")
    if data and isinstance(data, list):
        return [item["name"] for item in data if "name" in item]
    return []

@app.get("/health")
async def health():
    return {"status": "ok", "tokens": len(API_TOKENS)}

if __name__ == "__main__":
    import uvicorn
    logger.info(f"🚀 Minimalist Market Server starting on port {PORT}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
