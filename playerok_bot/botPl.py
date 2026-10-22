#!/usr/bin/env python3
import asyncio
import json
import os
import sys
import logging
import re
import html as html_lib
import time
import requests
import threading
import aiohttp

from playerok_bot.ai._dotenv import load_env

load_env()
import httpx
from playerok_bot.ai import airequests
from playerok_bot.utils import get_slug, format_time_ago, clean_text, format_price_rub, load_json_safe, save_json_atomic
from urllib.parse import quote  # НУЖНО ДЛЯ РУЧНОЙ КОДИРОВКИ ССЫЛКИ
from typing import Dict, Optional, List, Set
from concurrent.futures import ThreadPoolExecutor

from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, InputMediaPhoto

from playerok_bot.control.bot_control import register_control, check_group_access
from playerok_bot.control import app_settings

# Windows: консоль по умолчанию cp866/cp1251 — ломает вывод кириллицы и эмодзи
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise SystemExit(
        "BOT_TOKEN is not set. Copy .env.example to .env and fill it in."
    )

# Файлы
JSON_FILE = "lots_historyPl.json"
BLACKLIST_FILE = "blacklistedPl.json"

# Настройки API PlayerOK
GAME_ID = "1ed5d49c-9e13-69d0-3089-fe18e371a177"
CATEGORY_ID = "1efcc43d-d46b-6b10-9805-49c1107e28b3"

# Хеши
QUERY_HASH_ITEMS = "63eefcfd813442882ad846360d925279bc376e8bc85a577ebefbee0f9c78b557"
QUERY_HASH_DETAILS = "37d2d9f947e950c09322e2f5e3056451ee5f12dc38565eb811423e915c094c22"

REQUEST_DELAY = 1.5

# COOKIE (Из твоего рабочего примера)
# Cookies from a logged-in playerok.com session (see README).
COOKIE = os.getenv("PLAYEROK_COOKIE", "").strip()
if not COOKIE:
    raise SystemExit(
        "PLAYEROK_COOKIE is not set. Copy .env.example to .env and fill it in."
    )

# Крипта
CRYPTO_UPDATE_INTERVAL = 120
COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price?ids=the-open-network&vs_currencies=rub,usd"

# Настройки
MIN_PRICE = 10.00         
PRICE_CHANGE_THRESHOLD = 50.0 
FILTER_ENABLED = True
MAX_FORBIDDEN_CHARS = 2 
_group_raw = os.getenv("NOTIFICATION_GROUP_ID", "").strip()
NOTIFICATION_GROUP_ID = int(_group_raw) if _group_raw else 0
if not NOTIFICATION_GROUP_ID:
    raise SystemExit(
        "NOTIFICATION_GROUP_ID is not set. Copy .env.example to .env and fill it in."
    )
MARKET_SERVER = "http://127.0.0.1:9321"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("playerok_bot")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
executor = ThreadPoolExecutor(max_workers=4) 

# Команды управления ботами: /mode, /mode <режим>, /bots
register_control(dp, bot)

CURRENT_RATES = {"ton_rub": 0.0, "ton_usd": 0.0}
HISTORY_DATA: Dict[str, dict] = {}
BLACKLIST_DATA = {"sellers": set(), "lots": set()}
GROUP_WARNED = False
COOKIE_BLOCKED = False
HISTORY_CHANGED = False
MESSAGE_TO_LOT: Dict[str, dict] = {}
LOT_ANALYSIS_CACHE: Dict[str, dict] = {}
# Рыночные значения по лотам (для проверки выгоды)
LOT_MARKET: Dict[str, dict] = {}
DETAIL_CACHE: Dict[str, tuple] = {}

FORBIDDEN_CHARS_REGEX = re.compile(r"[^a-zA-Zа-яА-ЯёЁ0-9\s\-_.,!?:;()\[\]+=\"'/|%№$€₽*@\\]")

def count_forbidden_chars(text: Optional[str]) -> int:
    if not text: return 0
    return len(FORBIDDEN_CHARS_REGEX.findall(text))

# --- API CLIENT (Manual URL Construction) ---
class PlayerOkAPI:
    def __init__(self):
        self.session = requests.Session()
        # PlayerOK агрессивно режет запросы (429). Держим один поток + интервал.
        self._lock = threading.Lock()
        self._last_request = 0.0
        self.min_interval = float(os.getenv("PLAYEROK_MIN_INTERVAL", "1.2"))
        self.max_retries = int(os.getenv("PLAYEROK_MAX_RETRIES", "4"))
        
        # Заголовки точь-в-точь из твоего рабочего скрипта
        self.headers = {
            "accept": "*/*",
            "accept-language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "apollo-require-preflight": "true",
            "apollographql-client-name": "web",
            "cache-control": "no-cache",
            "content-type": "application/json",
            "cookie": COOKIE,
            "pragma": "no-cache",
            "referer": "https://playerok.com/apps/telegram/nft?sort=price_ASC",
            "sec-ch-ua": '"Not(A:Brand";v="8", "Chromium";v="144", "Google Chrome";v="144"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
            "x-gql-op": "items",
            "x-gql-path": "/apps/[game-slug]/[game-category-slug]",
            "x-timezone-offset": "-180",
        }
        self.session.headers.update(self.headers)

    def fetch_items(self, cursor=None, limit=24):
        # 1. Формируем переменные
        variables = {
            "pagination": {"first": limit},
            "filter": {
                "gameId": GAME_ID,
                "gameCategoryId": CATEGORY_ID,
                "status": ["APPROVED"]
            },
            "sort": {"field": "price", "direction": "ASC"}
        }

        if cursor:
            variables["pagination"]["after"] = cursor

        extensions = {
            "persistedQuery": {
                "version": 1,
                "sha256Hash": QUERY_HASH_ITEMS
            }
        }

        # 2. Превращаем в JSON строки и кодируем для URL (quote)
        # separators=(',', ':') убирает лишние пробелы, чтобы было компактно, как в браузере
        var_str = quote(json.dumps(variables, separators=(',', ':')))
        ext_str = quote(json.dumps(extensions, separators=(',', ':')))

        # 3. Собираем жесткую ссылку вручную
        full_url = (
            f"https://playerok.com/graphql"
            f"?operationName=items"
            f"&variables={var_str}"
            f"&extensions={ext_str}"
        )
        
        return self._make_request(full_url)

    def fetch_item_details(self, slug):
        variables = {
            "slug": slug,
            "hasSupportAccess": False,
            "showForbiddenImage": True
        }
        
        extensions = {
            "persistedQuery": {
                "version": 1,
                "sha256Hash": QUERY_HASH_DETAILS
            }
        }

        # То же самое: кодируем вручную
        var_str = quote(json.dumps(variables, separators=(',', ':')))
        ext_str = quote(json.dumps(extensions, separators=(',', ':')))

        full_url = (
            f"https://playerok.com/graphql"
            f"?operationName=item"
            f"&variables={var_str}"
            f"&extensions={ext_str}"
        )
        
        return self._make_request(full_url)

    def _throttle(self):
        """Один поток запросов + минимальный интервал (PlayerOK режет по 429)."""
        with self._lock:
            delta = time.time() - self._last_request
            if delta < self.min_interval:
                time.sleep(self.min_interval - delta)
            self._last_request = time.time()

    def _make_request(self, full_url):
        for attempt in range(self.max_retries):
            try:
                self._throttle()
                # Важно: передаем full_url как есть, без params=...
                response = self.session.get(full_url, timeout=15)

                if response.status_code == 200:
                    data = response.json()
                    if "errors" in data:
                        logger.error(f"API Error: {data['errors'][0].get('message')}")
                        return None
                    return data
                if response.status_code == 403:
                    global COOKIE_BLOCKED
                    if "DDoS-Guard" in (response.text or ""):
                        if not COOKIE_BLOCKED:
                            COOKIE_BLOCKED = True
                            logger.error(
                                "🚫 PlayerOK отдаёт 403 (DDoS-Guard) — куки в botPl.py "
                                "(COOKIE, поле cf_clearance) протухли для этого IP. "
                                "Обнови куки из браузера на playerok.com, иначе детали лотов "
                                "(фото/описание) не будут приходить."
                            )
                    else:
                        logger.error(f"HTTP 403: {response.text[:150]}")
                    return None
                if response.status_code == 429:
                    wait = 3.0 * (attempt + 1)
                    logger.warning(f"HTTP 429 (rate limit), повтор через {wait:.0f}s (попытка {attempt + 1}/{self.max_retries})")
                    time.sleep(wait)
                    continue
                if response.status_code >= 500:
                    wait = 2.0 * (attempt + 1)
                    logger.warning(f"HTTP {response.status_code}, повтор через {wait:.0f}s")
                    time.sleep(wait)
                    continue

                logger.error(f"HTTP Error {response.status_code}: {response.text[:200]}")
                return None
            except Exception as e:
                logger.error(f"Request Exception: {e}")
                time.sleep(1.5)
        return None

api_client = PlayerOkAPI()

# --- UTILS (Остальное без изменений) ---
def load_json_sync(path: str, default_type=dict):
    return load_json_safe(path, default_type())

def save_json_sync(path: str, data):
    save_json_atomic(path, data)

async def save_history_async():
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(executor, save_json_sync, JSON_FILE, HISTORY_DATA)

async def save_blacklist_async():
    loop = asyncio.get_running_loop()
    to_save = {
        "sellers": list(BLACKLIST_DATA["sellers"]),
        "lots": list(BLACKLIST_DATA["lots"])
    }
    await loop.run_in_executor(executor, save_json_sync, BLACKLIST_FILE, to_save)

async def crypto_updater_task():
    global CURRENT_RATES
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(COINGECKO_URL, timeout=10) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        tn = data.get("the-open-network", {})
                        if tn.get("rub") and tn.get("usd"):
                            CURRENT_RATES["ton_rub"] = float(tn["rub"])
                            CURRENT_RATES["ton_usd"] = float(tn["usd"])
            except Exception as e:
                logger.error(f"Crypto update error: {e}")
            await asyncio.sleep(CRYPTO_UPDATE_INTERVAL)

async def call_market_server(collection: str, model: str = "", backdrop: str = "", target_market: str = "") -> Optional[dict]:
    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            resp = await http.post(
                f"{MARKET_SERVER}/search_all",
                json={"collection": collection, "model": model, "backdrop": backdrop, "target_market": target_market}
            )
            if resp.status_code == 200:
                return resp.json()
    except Exception as e:
        logger.error(f"[MARKET_SRV] ❌ {e}")
    return None

def get_search_params(gemini_res: dict):
    coll    = gemini_res.get("collection", "none")
    model   = gemini_res.get("model", "none")
    backdrop = gemini_res.get("backdrop", "none")
    ids_str = gemini_res.get("ids", "none")
    
    if coll == "none" or not coll:
        return "", "", "", ""
    
    res_model = model if model not in ["none", "Default", ""] else ""
    res_backdrop = backdrop if backdrop not in ["none", "Default", ""] else ""
    res_ids = ids_str if ids_str not in ["none", "Default", ""] else ""
    
    return coll, res_model, res_backdrop, res_ids

async def call_gift_info(collection: str, gift_id: str) -> Optional[dict]:
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.post(
                f"{MARKET_SERVER}/gift_info",
                json={"collection": collection, "gift_id": gift_id}
            )
            if resp.status_code == 200:
                data = resp.json()
                if "error" not in data: return data
    except: pass
    return None

async def validate_metadata(collection: str, model: str, backdrop: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(f"{MARKET_SERVER}/validate_metadata", json={
                "collection": collection, "model": model, "backdrop": backdrop
            })
            if resp.status_code == 200:
                return resp.json()
    except: pass
    return {"collection": collection, "model": model, "backdrop": backdrop, "exists": False, "slug": get_slug(collection)}

def build_price_str(price: float, price_diff: Optional[float] = None) -> str:
    parts = [f"{format_price_rub(price)} ₽"]
    if price_diff:
        sign = "+" if price_diff > 0 else ""
        parts[0] += f" ({sign}{format_price_rub(price_diff)})"
    tr, tu = CURRENT_RATES["ton_rub"], CURRENT_RATES["ton_usd"]
    if tr > 0 and tu > 0:
        usd_rub = tr / tu
        parts.append(f"{price / tr:.2f} TON ({tr:.2f})")
        parts.append(f"${price / usd_rub:.2f} ({usd_rub:.2f})")
    return " | ".join(parts)

def build_initial_caption(lot: dict, price_diff: Optional[float]) -> str:
    title     = html_lib.escape(lot.get("title") or "Лот")
    offer_url = lot.get("offer_url", "")
    price     = lot.get("price")
    seller    = html_lib.escape(lot.get("seller_name", ""))
    reviews   = lot.get("reviews", 0)
    price_str = build_price_str(price, price_diff) if price else "—"
    return "\n".join([
        f"<b><a href='{offer_url}'>{title}</a></b>",
        f"💵 {price_str}",
        f"{seller} | ⭐ {reviews}",
        "",
        "🔍 <i>Анализирую подарок...</i>",
    ])

def build_final_caption(lot: dict, price_diff: Optional[float], price_info: str) -> str:
    title     = html_lib.escape(lot.get("title") or "Лот")
    offer_url = lot.get("offer_url", "")
    price     = lot.get("price")
    seller    = html_lib.escape(lot.get("seller_name", ""))
    reviews   = lot.get("reviews", 0)
    price_str = build_price_str(price, price_diff) if price else "—"
    lines = [
        f"<b><a href='{offer_url}'>{title}</a></b>",
        f"💵 {price_str}",
        f"{seller} | ⭐ {reviews}",
    ]
    if price_info:
        lines += ["", price_info]
    return "\n".join(lines)

def get_page_keyboard(seller: str, lot_id: str, page: str = "main", has_detail: bool = False) -> InlineKeyboardMarkup:
    rows = []
    if page == "main" and has_detail:
        rows.append([InlineKeyboardButton(text="📊 Подробная информация", callback_data=f"detail:{lot_id}")])
    elif page == "detail":
        rows.append([InlineKeyboardButton(text="🛒 В продаже (Маркет)", callback_data=f"sales:{lot_id}")])
    elif page == "sales":
        rows.append([InlineKeyboardButton(text="📊 Сделки (Активность)", callback_data=f"detail:{lot_id}")])
        
    safe_seller = seller[:20] if seller else "user"
    rows.append([
        InlineKeyboardButton(text="🚫 Продавец", callback_data=f"ban_user:{safe_seller}"),
        InlineKeyboardButton(text="❌ Лот",      callback_data=f"ban_lot:{lot_id}"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def build_detail_text(lot: dict, gemini_res: dict, items_info: List[dict]) -> str:
    price_fp = lot.get("price", 0)
    tr, tu = CURRENT_RATES["ton_rub"], CURRENT_RATES["ton_usd"]
    
    usd_rub = (tr / tu) if (tr > 0 and tu > 0) else 105.0
    price_with_fee = price_fp + 3.5 * usd_rub
    
    total_avg_ton = 0.0
    total_avg_rub = 0.0
    is_multi = len(items_info) > 1
    
    first_item = items_info[0] if items_info else {}
    c0 = first_item.get("collection") or ""
    m0, b0 = first_item.get("model") or "", first_item.get("backdrop") or ""
    slug0 = first_item.get("slug", "")
    gid0 = first_item.get("gid", "")
    
    id_part = ""
    if gid0 and slug0:
        url = f"https://t.me/nft/{slug0}-{gid0}"
        id_part = f" | <a href='{url}'>#{gid0}</a>"
    elif gemini_res.get("ids", "none") != "none":
        id_val = str(gemini_res.get("ids").split(",")[0].strip()).replace("#", "")
        id_part = f" | #{id_val}"
    
    header_parts = [c0]
    if m0: header_parts.append(m0)
    if b0: header_parts.append(b0)
    header_name = " | ".join(header_parts)
    
    header = [
        "<b>📊 Детальная информация</b>",
        f"<b>{header_name}{id_part}</b>",
        f"💰 +TRC20 3.5$: <b>{format_price_rub(price_with_fee)} ₽</b>",
        ""
    ]
    
    lines = header.copy()

    for item in items_info:
        c, m, b = item.get("collection", ""), item["model"], item["backdrop"]
        pd = item["price_data"]
        stats = pd.get("activity_stats", {})
        on_sale = pd.get("on_sale", [])
        
        avg_ton = stats.get("median") or stats.get("avg") or (on_sale[0]["price"] if on_sale else 0)
        avg_rub = avg_ton * tr if tr > 0 else 0
        total_avg_ton += avg_ton
        total_avg_rub += avg_rub
        
        if is_multi:
            parts = []
            if m: parts.append(m)
            if b: parts.append(b)
            p_text = "🔹 " + " | ".join(parts) if parts else "🔹 Лот"
            lines.append(p_text)
            if not stats:
                lines.append("  Нет данных о ценах")
            lines.append("")
        else:
            if not stats:
                lines.append("<i>Данные о ценах не найдены</i>")

    all_activity = []
    for item in items_info:
        c, m, b = item.get("collection") or "", item.get("model") or "", item.get("backdrop") or ""
        pd = item.get("price_data", {})
        
        meta = c if c else "Lot"
        if m: meta += f" | {m}"
        if b: meta += f" | {b}"
            
        for act in pd.get("activity", []):
            act["_meta"] = meta
            all_activity.append(act)
            
    all_activity.sort(key=lambda x: x.get("sold_at", ""), reverse=True)
    
    if all_activity:
        lines += ["", "<b>Сделки (активность):</b>"]
        for a in all_activity[:4]:
            a_price = a.get("price", 0)
            a_time = format_time_ago(a.get("sold_at", ""))
            a_market = a.get("market", "?")
            tg_id = a.get("tg_id", "")
            meta = a["_meta"]
            short_meta = meta[:25] + ".." if len(meta) > 25 else meta
            if tg_id:
                lines.append(f"  • [{short_meta}] <a href='https://t.me/nft/{tg_id}'>{a_price:.2f} TON</a> ({a_market}) — {a_time}")
            else:
                lines.append(f"  • [{short_meta}] {a_price:.2f} TON ({a_market}) — {a_time}")

    return "\n".join(lines)

def build_sales_text(items_info: List[dict]) -> str:
    all_on_sale = []
    for item in items_info:
        c, m, b = item.get("collection") or "", item.get("model") or "", item.get("backdrop") or ""
        meta = c if c else "Lot"
        if m: meta += f" | {m}"
        if b: meta += f" | {b}"
        for sale in item.get("price_data", {}).get("on_sale", []):
            sale["_meta"] = meta
            all_on_sale.append(sale)
            
    all_on_sale.sort(key=lambda x: float(x.get("price", 0)))
    
    lines = ["<b>🛒 Текущие на продаже (Маркет):</b>"]
    if all_on_sale:
        for sale in all_on_sale[:15]:
            s_price = sale.get("price", 0)
            s_market = sale.get("market", "?")
            tg_id = sale.get("tg_id", "")
            meta = sale["_meta"]
            short_meta = meta[:25] + ".." if len(meta) > 25 else meta
            if tg_id:
                lines.append(f"  • [{short_meta}] <a href='https://t.me/nft/{tg_id}'>{s_price:.2f} TON</a> ({s_market})")
            else:
                lines.append(f"  • [{short_meta}] {s_price:.2f} TON ({s_market})")
    else:
        lines.append("  • Предложений не найдено.")
    return "\n".join(lines)

def merge_market_results(list_a: list, list_b: list) -> list:
    if not list_a and not list_b: return []
    if not list_a: return list_b
    if not list_b: return list_a
    
    merged = []
    for i in range(len(list_a)):
        ra = list_a[i] or {}
        rb = list_b[i] or {}
        
        on_sale = ra.get("on_sale", []) + rb.get("on_sale", [])
        activity = ra.get("activity", []) + rb.get("activity", [])
        
        on_sale.sort(key=lambda x: float(x.get("price", 0)))
        activity.sort(key=lambda x: x.get("sold_at", ""), reverse=True)
        prices = sorted([float(a["price"]) for a in activity if float(a.get("price", 0)) > 0])
        
        stats = {}
        if prices:
            n = len(prices)
            trim = max(0, int(n * 0.1))
            trimmed = prices[trim:n-trim] if n > 4 else prices
            avg = sum(trimmed) / len(trimmed) if trimmed else 0
            stats = {
                "min": round(prices[0], 2),
                "max": round(prices[-1], 2),
                "avg": round(avg, 2)
            }
            
        merged.append({
            "on_sale": on_sale,
            "activity": activity,
            "activity_stats": stats
        })
    return merged

async def process_notification(lot_basic: dict, diff: Optional[float]):
    seller = lot_basic.get("seller_name", "")
    lot_id = lot_basic.get("id", "")
    slug = lot_basic.get("slug", "")

    if seller in BLACKLIST_DATA["sellers"] or lot_id in BLACKLIST_DATA["lots"]:
        return

    loop = asyncio.get_running_loop()
    details_data = await loop.run_in_executor(executor, api_client.fetch_item_details, slug)
    
    images = []
    long_desc = ""
    lot = lot_basic.copy()
    lot["reviews"] = 0

    if details_data and 'data' in details_data and 'item' in details_data['data']:
        item = details_data['data']['item']
        
        attachments = item.get('attachments', [])
        for att in attachments:
            if att.get('url'):
                images.append(att['url'])
        
        long_desc = item.get("description", "")
        lot["long_desc"] = long_desc
        
        user_data = item.get('user', {})
        if user_data:
            lot['rating'] = user_data.get('rating', lot.get('rating', 0))
            lot['reviews'] = user_data.get('testimonialCounter', 0)
            if user_data.get('username'):
                lot['seller_name'] = user_data['username']

    if not images and lot.get("image_url"):
        images.append(lot["image_url"])

    price_fp = lot.get("price")
    if price_fp is None: return
    
    initial = build_initial_caption(lot, diff)
    kb = get_page_keyboard(lot['seller_name'], lot_id, "main")
    sent_messages = []

    try:
        if len(images) > 1:
            media = [types.InputMediaPhoto(media=img) for img in images[:10]]
            msgs = await bot.send_media_group(chat_id=NOTIFICATION_GROUP_ID, media=media)
            for m_item in msgs:
                MESSAGE_TO_LOT[str(m_item.message_id)] = lot
            
            m = await bot.send_message(NOTIFICATION_GROUP_ID, initial, reply_markup=kb, disable_web_page_preview=True)
            sent_messages.append((NOTIFICATION_GROUP_ID, m.message_id, False))
            MESSAGE_TO_LOT[str(m.message_id)] = lot
        elif len(images) == 1:
            m = await bot.send_photo(NOTIFICATION_GROUP_ID, images[0], caption=initial, reply_markup=kb)
            sent_messages.append((NOTIFICATION_GROUP_ID, m.message_id, True))
            MESSAGE_TO_LOT[str(m.message_id)] = lot
        else:
            m = await bot.send_message(NOTIFICATION_GROUP_ID, initial, reply_markup=kb, disable_web_page_preview=True)
            sent_messages.append((NOTIFICATION_GROUP_ID, m.message_id, False))
            MESSAGE_TO_LOT[str(m.message_id)] = lot
    except Exception as e:
        global GROUP_WARNED
        if not GROUP_WARNED:
            GROUP_WARNED = True
            logger.error(
                f"⚠️ Не удалось отправить в чат {NOTIFICATION_GROUP_ID}: {e}\n"
                f"   Бот не состоит в группе (или нет прав на отправку). "
                f"Добавь бота в группу и выдай права администратора. "
                f"Дальнейшие ошибки отправки будут залогированы как DEBUG."
            )
        else:
            logger.debug(f"Send error group: {e}")

    gemini_res = None
    detailed_text = ""
    price_info_text = ""
    import hashlib
    data_hash = f"{lot.get('title', '')}_{''.join(images)}_{long_desc}"
    info_hash = hashlib.md5(data_hash.encode('utf-8')).hexdigest()

    if info_hash in LOT_ANALYSIS_CACHE:
        gemini_res = LOT_ANALYSIS_CACHE[info_hash]
    elif images or long_desc:
        gemini_res = await airequests.call_gemini_server(images, lot.get("title", ""), long_desc)
        if gemini_res:
            LOT_ANALYSIS_CACHE[info_hash] = gemini_res

    if gemini_res and not gemini_res.get("error"):
        raw_coll, raw_model_str, raw_backdrop_str, raw_ids_str = get_search_params(gemini_res)
        
        if (not raw_coll or raw_coll == "none") and raw_model_str and raw_model_str != "none":
            raw_coll = raw_model_str
        
        if not raw_coll:
            price_info_text = "⚠️ Не удалось распознать конкретные предметы (множество NFT)"
            # Цену не оценили - уведомление бесполезно, не отправляем мусор
            if app_settings.skip_no_price_data_enabled():
                logger.info(f"💸 Лот {lot_id} пропущен: {price_info_text}")
                for uid, mid, is_photo in sent_messages:
                    try:
                        await bot.delete_message(chat_id=uid, message_id=mid)
                    except Exception:
                        pass
                return
            final_caption = build_final_caption(lot, diff, price_info_text)
            final_kb = get_page_keyboard(lot['seller_name'], lot_id, "main")
            for uid, mid, is_photo in sent_messages:
                try:
                    if is_photo:
                        await bot.edit_message_caption(chat_id=uid, message_id=mid, caption=final_caption, reply_markup=final_kb)
                    else:
                        await bot.edit_message_text(chat_id=uid, message_id=mid, text=final_caption, reply_markup=final_kb, disable_web_page_preview=True)
                except Exception: pass
            return

        colls = [c.strip() for c in raw_coll.split(",") if c.strip()]
        models = [m.strip() for m in raw_model_str.split(",") if m.strip()]
        backdrops = [b.strip() for b in raw_backdrop_str.split(",") if b.strip()]
        ids = [i.strip() for i in raw_ids_str.split(",") if i.strip()]
        
        n_items = max(len(colls), len(models), len(backdrops), len(ids))
        if n_items == 0: n_items = 1
        
        if not gemini_res.get("multi_gift", False):
            n_items = 1
        
        while len(colls) < n_items: colls.append(colls[-1] if colls else "")
        while len(models) < n_items: models.append(models[0] if models else "")
        while len(backdrops) < n_items: backdrops.append(backdrops[0] if backdrops else "")
        while len(ids) < n_items: ids.append("")
        
        actual_items = []
        for i in range(n_items):
            c = colls[i] if i < len(colls) else (colls[0] if colls else "")
            m, b, gid = models[i], backdrops[i], ids[i]
            clean_gid = gid.replace("#", "").strip()
            
            item_val = await validate_metadata(c, m, b)
            m, b = item_val.get("model", m), item_val.get("backdrop", b)
            slug_c = item_val.get("slug", get_slug(c))
            coll_exists = item_val.get("exists", False)

            if clean_gid and coll_exists:
                info = await call_gift_info(c, clean_gid)
                if info:
                    m = info.get("model", m)
                    b = info.get("backdrop", b)

            if "black" in (b or "").lower():
                search_b, search_m = b, ""
            else:
                search_b, search_m = "", m
            
            res_item = {"c": c, "m": search_m, "b": search_b, "gid": clean_gid, "slug": slug_c}
            if res_item not in actual_items:
                actual_items.append(res_item)

        actual_items = actual_items[:5]

        async def fetch_market_loop(target_market: str) -> tuple:
            results = []
            for it in actual_items:
                res = await call_market_server(it["c"], it["m"], it["b"], target_market)
                results.append(res)
                await asyncio.sleep(0.5)
            return target_market, results

        market_data = {"mrkt": [], "portals": []}
        merged_results = []
        
        async def _update_ui(current_merged: list, is_final: bool = False):
            items_info = []
            total_avg_ton = 0.0
            total_max_ton = 0.0
            total_min_ton = 0.0
            found_any = False
            items_with_data = 0      # предметов, по которым нашли цену
            items_with_history = 0   # предметов, где цена посчитана по реальным продажам

            for i, res in enumerate(current_merged):
                c_used, m_used, b_used = actual_items[i]["c"], actual_items[i]["m"], actual_items[i]["b"]
                if res:
                    items_info.append({
                        "collection": c_used,
                        "model": m_used, 
                        "backdrop": b_used, 
                        "price_data": res,
                        "slug": actual_items[i]["slug"],
                        "gid": actual_items[i]["gid"]
                    })
                    st = res.get("activity_stats", {})
                    if st:
                        total_avg_ton += st.get("avg", 0)
                        total_max_ton += st.get("max", 0)
                        total_min_ton += st.get("min", 0)
                        found_any = True
                        items_with_data += 1
                        items_with_history += 1
                    elif res.get("on_sale"):
                        # Только минимальная цена в продаже - это НЕ реальная
                        # цена продажи, профит по ней гарантировать нельзя
                        floor = res["on_sale"][0]["price"]
                        total_avg_ton += floor
                        found_any = True
                        items_with_data += 1
            
            profit_check_res = None
            price_certain = False
            if found_any:
                tr, tu = CURRENT_RATES["ton_rub"], CURRENT_RATES["ton_usd"]
                usd_rub = (tr / tu) if (tr > 0 and tu > 0) else 105.0
                price_with_fee = price_fp + 3.5 * usd_rub

                total_avg_rub = total_avg_ton * tr if tr > 0 else 0
                profit = total_avg_rub - price_fp
                profit_str = f"+{format_price_rub(profit)} ₽" if profit >= 0 else f"-{format_price_rub(abs(profit))} ₽"
                
                profit_trc20 = total_avg_rub - price_with_fee
                profit_trc20_str = f"+{format_price_rub(profit_trc20)} ₽" if profit_trc20 >= 0 else f"-{format_price_rub(abs(profit_trc20))} ₽"

                summary = []
                if len(actual_items) > 1:
                    summary.append(f"📦 <b>Предметов:</b> {len(actual_items)}")
                
                has_history = total_max_ton > 0
                label = "Средняя" if has_history else "В продаже (Floor)"
                
                summary.append(f"📊 <b>{label}:</b> {total_avg_ton:.2f} TON | {format_price_rub(total_avg_rub)} ₽")
                if has_history:
                    summary.append(f"📉 {total_min_ton:.2f} | 📈 {total_max_ton:.2f} TON")
                summary.append(f"💰 <b>Профит:</b> {profit_str} | TRC20: {profit_trc20_str}")
                
                if not is_final:
                    summary.append("\n⏳ <i>Сбор данных с остальных маркетов...</i>")
                
                price_info_text = "\n".join(summary)
                detailed_text = build_detail_text(lot, gemini_res, items_info)
                sales_text = build_sales_text(items_info)

                # Цена определена по реальным продажам всех предметов - профит гарантирован
                price_certain = items_with_data > 0 and items_with_history == items_with_data

                LOT_MARKET[lot_id] = {
                    "avg_ton": total_avg_ton,
                    "avg_rub": total_avg_rub,
                    "price_rub": price_fp,
                    "profit_rub": profit,
                    "profit_net_rub": profit_trc20,
                    "fee_rub": max(0.0, price_with_fee - price_fp),
                    "certain": price_certain,
                }
                profit_check_res = app_settings.profit_check(
                    price_fp, total_avg_rub, fee_rub=max(0.0, price_with_fee - price_fp)
                )
            else:
                price_info_text = "📊 Данные о ценах не найдены" if is_final else "⏳ <i>Ищем данные на маркетах...</i>"
                detailed_text = ""
                sales_text = ""

            final_caption = build_final_caption(lot, diff, price_info_text)
            final_kb = get_page_keyboard(lot['seller_name'], lot_id, "main", has_detail=bool(detailed_text))

            if detailed_text or sales_text:
                DETAIL_CACHE[lot_id] = (final_caption, detailed_text, sales_text, lot['seller_name'], time.time())

            # Фильтр мусора: без данных о ценах не показываем, в режиме
            # "только выгодные" - только с гарантированным профитом
            if is_final:
                market_rub = total_avg_rub if found_any else 0.0
                fee_rub = max(0.0, price_with_fee - price_fp) if found_any else 0.0
                certain = bool(price_certain) if found_any else False
                show, reason = app_settings.should_notify(
                    price_fp, market_rub, fee_rub=fee_rub, certain=certain
                )
                if not show:
                    logger.info(f"\U0001f4b8 Лот {lot_id} пропущен: {reason}")
                    for uid, mid, is_photo in sent_messages:
                        try:
                            await bot.delete_message(chat_id=uid, message_id=mid)
                        except Exception:
                            pass
                    sent_messages.clear()
                    return

            for uid, mid, is_photo in sent_messages:
                try:
                    if is_photo:
                        await bot.edit_message_caption(chat_id=uid, message_id=mid, caption=final_caption, reply_markup=final_kb)
                    else:
                        await bot.edit_message_text(chat_id=uid, message_id=mid, text=final_caption, reply_markup=final_kb, disable_web_page_preview=True)
                except Exception: pass

        task1 = asyncio.create_task(fetch_market_loop("mrkt"))
        task2 = asyncio.create_task(fetch_market_loop("portals"))
        
        for task in asyncio.as_completed([task1, task2]):
            m_name, res_list = await task
            market_data[m_name] = res_list
            merged_results = merge_market_results(market_data["mrkt"], market_data["portals"])
            await _update_ui(merged_results, is_final=False)
            
        needs_fallback = False
        for i, res in enumerate(merged_results):
            if not res or (not res.get("activity_stats") and not res.get("on_sale")):
                it = actual_items[i]
                if it["m"] or it["b"]:
                    fallback_res = await call_market_server(it["c"], "", "")
                    merged_results[i] = fallback_res
                    it["m"], it["b"] = "Floor", ""
                    needs_fallback = True
                    
        await _update_ui(merged_results, is_final=True)
        return

    elif gemini_res and gemini_res.get("error"):
        price_info_text = "⚠️ Не получилось извлечь данные о Telegram NFT"
        # Цену оценить не удалось - уведомление без смысла, не отправляем мусор
        if app_settings.skip_no_price_data_enabled():
            logger.info(f"💸 Лот {lot_id} пропущен: {price_info_text}")
            for uid, mid, is_photo in sent_messages:
                try:
                    await bot.delete_message(chat_id=uid, message_id=mid)
                except Exception:
                    pass
            return
        final_caption = build_final_caption(lot, diff, price_info_text)
        final_kb = get_page_keyboard(lot['seller_name'], lot_id, "main")
        for uid, mid, is_photo in sent_messages:
            try:
                if is_photo:
                    await bot.edit_message_caption(chat_id=uid, message_id=mid, caption=final_caption, reply_markup=final_kb)
                else:
                    await bot.edit_message_text(chat_id=uid, message_id=mid, text=final_caption, reply_markup=final_kb, disable_web_page_preview=True)
            except Exception: pass


async def watcher_loop():
    global HISTORY_DATA, HISTORY_CHANGED, BLACKLIST_DATA
    
    raw_hist = load_json_sync(JSON_FILE, dict)
    if isinstance(raw_hist, list): 
        HISTORY_DATA = {str(i.get("id")): i for i in raw_hist if "id" in i}
    else:
        HISTORY_DATA = raw_hist
        
    raw_bl = load_json_sync(BLACKLIST_FILE, dict)
    BLACKLIST_DATA["sellers"] = set(raw_bl.get("sellers", []))
    BLACKLIST_DATA["lots"] = set(raw_bl.get("lots", []))

    is_first_run_session = len(HISTORY_DATA) == 0
    if is_first_run_session:
        logger.info("История пуста. Первый проход (без уведомлений).")
    else:
        logger.info(f"В базе {len(HISTORY_DATA)} лотов. Мониторинг активен.")

    loop = asyncio.get_running_loop()
    current_cursor = None

    while True:
        try:
            data = await loop.run_in_executor(executor, api_client.fetch_items, current_cursor)
            
            if data and 'data' in data and 'items' in data['data']:
                items_block = data['data']['items']
                edges = items_block.get('edges', [])
                page_info = items_block.get('pageInfo', {})
                
                HISTORY_CHANGED = False
                notification_tasks = []

                for edge in edges:
                    node = edge['node']
                    
                    lot_id = node['id']
                    price = float(node.get('price', 0))
                    title = node.get('name', 'No Name')
                    slug = node.get('slug', '')
                    
                    user_node = node.get('user', {})
                    seller_name = user_node.get('username', 'Unknown')
                    seller_rating = user_node.get('rating', 0)
                    
                    attach = node.get('attachment')
                    image_url = attach.get('url') if attach else None
                    
                    offer_url = f"https://playerok.com/products/{slug}"

                    lot_obj = {
                        "id": lot_id,
                        "title": title,
                        "price": price,
                        "seller_name": seller_name,
                        "rating": seller_rating,
                        "offer_url": offer_url,
                        "image_url": image_url,
                        "slug": slug
                    }

                    if price < MIN_PRICE: continue
                    if FILTER_ENABLED and count_forbidden_chars(title) > MAX_FORBIDDEN_CHARS: continue
                    if seller_name in BLACKLIST_DATA["sellers"] or lot_id in BLACKLIST_DATA["lots"]: continue

                    old_lot = HISTORY_DATA.get(lot_id)
                    
                    if old_lot:
                        old_price = old_lot.get("price", 0)
                        if abs(price - old_price) > 0.01:
                            HISTORY_DATA[lot_id] = lot_obj
                            HISTORY_CHANGED = True
                            diff = price - old_price
                            if not is_first_run_session and abs(diff) >= PRICE_CHANGE_THRESHOLD:
                                notification_tasks.append(process_notification(lot_obj, diff))
                    else:
                        HISTORY_DATA[lot_id] = lot_obj
                        HISTORY_CHANGED = True
                        if not is_first_run_session:
                            notification_tasks.append(process_notification(lot_obj, None))

                if notification_tasks:
                    logger.info(f"Обработка {len(notification_tasks)} новых лотов...")
                    await asyncio.gather(*notification_tasks)

                if HISTORY_CHANGED:
                    asyncio.create_task(save_history_async())

                has_next = page_info.get('hasNextPage', False)
                end_cursor = page_info.get('endCursor')

                if has_next and end_cursor:
                    current_cursor = end_cursor
                    await asyncio.sleep(REQUEST_DELAY)
                else:
                    logger.info("Цикл завершен. Рестарт с 1 страницы.")
                    current_cursor = None
                    if is_first_run_session:
                        is_first_run_session = False
                        logger.info("Первичный сбор завершен.")
                    await asyncio.sleep(REQUEST_DELAY)

            else:
                logger.warning("Пустой ответ API. Ждем...")
                await asyncio.sleep(5)

        except Exception as e:
            logger.error(f"Ошибка Watcher: {e}")
            await asyncio.sleep(5)

# --- HANDLERS ---
@dp.callback_query(F.data.startswith("ban_"))
async def callback_ban(call: CallbackQuery):
    action, value = call.data.split(":", 1)
    if action == "ban_user":
        BLACKLIST_DATA["sellers"].add(value)
        msg = f"Продавец '{value}' в ЧС."
    elif action == "ban_lot":
        BLACKLIST_DATA["lots"].add(value)
        msg = f"Лот скрыт."
    else:
        return
    asyncio.create_task(save_blacklist_async())
    await call.answer(msg, show_alert=False)

@dp.callback_query(F.data.startswith("detail:"))
async def callback_detail(call: CallbackQuery):
    lot_id = call.data.split(":", 1)[1]
    res = DETAIL_CACHE.get(lot_id)
    if res and len(res) >= 4:
        base_text, info_text, sales_text, seller = res[:4]
        try:
            new_text = f"{base_text}\n\n{info_text}"
            new_kb = get_page_keyboard(seller, lot_id, "detail")
            
            if call.message.photo:
                await call.message.edit_caption(caption=new_text, reply_markup=new_kb)
            else:
                await call.message.edit_text(text=new_text, reply_markup=new_kb, disable_web_page_preview=True)
        except Exception as e:
            logger.error(f"Edit detail error: {e}")
            await call.answer("❌ Ошибка при обновлении текста", show_alert=True)
    else:
        await call.answer("ℹ️ Подробная информация недоступна или устарела.", show_alert=True)
    await call.answer()

@dp.callback_query(F.data.startswith("sales:"))
async def callback_sales(call: CallbackQuery):
    lot_id = call.data.split(":", 1)[1]
    res = DETAIL_CACHE.get(lot_id)
    if res and len(res) >= 4:
        base_text, info_text, sales_text, seller = res[:4]
        try:
            new_text = f"{base_text}\n\n{sales_text}"
            new_kb = get_page_keyboard(seller, lot_id, "sales")
            
            if call.message.photo:
                await call.message.edit_caption(caption=new_text, reply_markup=new_kb)
            else:
                await call.message.edit_text(text=new_text, reply_markup=new_kb, disable_web_page_preview=True)
        except Exception as e:
            logger.error(f"Edit sales error: {e}")
            await call.answer("❌ Ошибка при обновлении текста", show_alert=True)
    else:
        await call.answer("ℹ️ Информация о продажах недоступна.", show_alert=True)


@dp.message(Command("list"))
async def cmd_list(msg: types.Message):
    sellers = list(BLACKLIST_DATA["sellers"])
    text = f"<b>🚫 Sellers ({len(sellers)}):</b>\n" + ", ".join(sellers)
    if len(text) > 4000: text = text[:4000] + "..."
    await msg.answer(text)

@dp.message(Command("del"))
async def cmd_del(msg: types.Message, command: CommandObject):
    args = command.args
    if not args: return
    target = args.strip()
    removed = []
    if target in BLACKLIST_DATA["sellers"]:
        BLACKLIST_DATA["sellers"].remove(target)
        removed.append("продавец")
    if target in BLACKLIST_DATA["lots"]:
        BLACKLIST_DATA["lots"].remove(target)
        removed.append("лот")
    if removed:
        asyncio.create_task(save_blacklist_async())
        await msg.answer(f"Удалено: {target} ({', '.join(removed)})")
    else:
        await msg.answer(f"Не найдено в ЧС: {target}")

@dp.message(Command("start"))
async def cmd_start(msg: types.Message):
    await msg.answer(
        "🤖 <b>PlayerOK Gift Watcher</b>\n\n"
        "Мониторинг PlayerOK + Gemini AI + Portals/MRKT\n\n"
        "<b>Команды:</b>\n"
        "/status — статус бота\n"
        "/list — черный список\n"
        "/del [имя] — удалить из ЧС"
    )

@dp.message(Command("status"))
async def cmd_status(msg: types.Message):
    ton = CURRENT_RATES.get("ton_rub", 0)
    await msg.answer(f"📊 <b>Статус</b>\nTON: {ton:.2f} ₽\nМин. цена: {MIN_PRICE} ₽")

async def main():
    asyncio.create_task(crypto_updater_task())
    asyncio.create_task(watcher_loop())

    warn = await check_group_access(bot, NOTIFICATION_GROUP_ID)
    if warn:
        logger.warning(warn)

    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass