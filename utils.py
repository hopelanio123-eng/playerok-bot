import html as html_lib
import json
import os
import re
import shutil
import time
from datetime import datetime, timezone
import asyncio
import httpx
import logging
from typing import Any, Optional
from urllib.parse import urlparse, parse_qs

logger = logging.getLogger("funpay_bot")

CURRENT_RATES = {"ton_rub": 0.0, "ton_usd": 0.0}
CRYPTO_UPDATE_INTERVAL = 120

async def crypto_updater_task():
    url = "https://tonapi.io/v2/rates?tokens=ton&currencies=usd,rub"
    async with httpx.AsyncClient(timeout=15.0) as http:
        while True:
            try:
                resp = await http.get(url)
                if resp.status_code == 200:
                    prices = resp.json().get("rates", {}).get("TON", {}).get("prices", {})
                    if prices.get("RUB"): CURRENT_RATES["ton_rub"] = float(prices["RUB"])
                    if prices.get("USD"): CURRENT_RATES["ton_usd"] = float(prices["USD"])
                    logger.info(f"💰 TON: {CURRENT_RATES['ton_rub']:.2f} ₽ | ${CURRENT_RATES['ton_usd']:.2f}")
            except Exception as e:
                logger.error(f"Crypto update error: {e}")
            await asyncio.sleep(CRYPTO_UPDATE_INTERVAL)

def get_slug(name: str) -> str:
    if not name: return ""
    return re.sub(r'[^a-zA-Z0-9]', '', name).lower()

def get_nft_link(coll: str, nft_id: str) -> str:
    slug = get_slug(coll)
    url = f"https://t.me/nft/{slug}-{nft_id}"
    return f"<a href='{url}'>#{nft_id}</a>"

FORBIDDEN_CHARS_REGEX = re.compile("[^a-zA-Zа-яА-ЯёЁ0-9\\s\\-_.,!?:;()\\[\\]+=\\\\\"'/|%№$€₽*@\\\\]")

def count_forbidden_chars(text: Optional[str]) -> int:
    if not text: return 0
    return len(FORBIDDEN_CHARS_REGEX.findall(text))

def extract_offer_id(href: str) -> Optional[str]:
    if not href: return None
    try:
        parsed = urlparse(href)
        qs = parse_qs(parsed.query)
        if "id" in qs: return qs["id"][0]
        for p in reversed(parsed.path.rstrip("/").split("/")):
            if p.isdigit(): return p
        return href
    except: return href

def clean_text(text: str) -> str:
    if not text: return ""
    text = html_lib.unescape(text)
    text = re.sub(r'<[^>]+>', '', text)
    return " ".join(text.split())

def format_price_rub(p: float) -> str:
    return f"{p:,.2f}".replace(",", " ").replace(".", ",") if p is not None else "—"

def format_time_ago(iso_date_str: str) -> str:
    try:
        dt = datetime.strptime(iso_date_str.split('.')[0], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        diff = datetime.now(timezone.utc) - dt
        secs = diff.total_seconds()
        if secs < 60:    return f"{int(secs)} сек"
        elif secs < 3600: return f"{int(secs // 60)} мин"
        elif secs < 86400: return f"{int(secs // 3600)} ч"
        else:             return f"{int(secs // 86400)} дн"
    except:
        return "?"


# =============================================================================
# --- БЕЗОПАСНАЯ РАБОТА С JSON-ФАЙЛАМИ ---
# =============================================================================
# Раньше при битом JSON файл молча заменялся пустым, и вся история лотов
# терялась безвозвратно. Теперь битый файл откладывается в .corrupt,
# а прошлый валидный — в .bak, и об ошибке пишется в лог.


def backup_corrupt_file(path: str) -> Optional[str]:
    """Откладывает нечитаемый JSON, чтобы он не потерялся."""
    try:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = f"{path}.corrupt_{stamp}"
        shutil.copy2(path, dest)
        logger.error(f"Файл {path} повреждён — копия сохранена в {dest}")
        return dest
    except Exception as exc:
        logger.error(f"Не удалось сохранить повреждённый {path}: {exc}")
        return None


def load_json_safe(path: str, default: Any = None) -> Any:
    """Читает JSON. При повреждении НЕ стирает файл, а откладывает его."""
    if not os.path.exists(path):
        return {} if default is None else default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as exc:
        logger.error(f"JSON битый в {path}: {exc}")
        backup_corrupt_file(path)
        return {} if default is None else default
    except Exception as exc:
        logger.error(f"Не удалось прочитать {path}: {exc}")
        return {} if default is None else default


def save_json_atomic(path: str, data: Any, keep_backup: bool = True) -> bool:
    """Пишет JSON атомарно (tmp + os.replace) и держит предыдущую версию в .bak.

    .bak обновляется не чаще раза в 10 минут, чтобы не гонять копирование
    многотысячных файлов на каждом цикле опроса.
    """
    tmp_path = f"{path}.tmp"
    bak_path = f"{path}.bak"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        if keep_backup and os.path.exists(path):
            try:
                stale = not os.path.exists(bak_path) or (
                    time.time() - os.path.getmtime(bak_path) > 600
                )
                if stale:
                    shutil.copy2(path, bak_path)
            except Exception:
                pass
        os.replace(tmp_path, path)
        return True
    except Exception as exc:
        logger.error(f"Ошибка сохранения {path}: {exc}")
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        return False
