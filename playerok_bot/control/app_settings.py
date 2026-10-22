# -*- coding: utf-8 -*-
"""
app_settings.py — общие настройки приложения (работают во всех ботах).

Хранятся в settings.json рядом с проектом, читаются/пишутся атомарно.

Ключи:
    only_profitable   — bool,   включить режим «только выгодные предложения»
    profit_margin_pct — float,  минимальная маржа выгоды, %
    min_profit_rub    — float,  минимальная прибыль в рублях
"""

import json
import os

from playerok_bot.ai._dotenv import load_env

load_env()
import shutil
from typing import Any, Dict, Tuple

from playerok_bot.paths import PROJECT_DIR

SETTINGS_FILE = os.path.join(PROJECT_DIR, "settings.json")

DEFAULT_SETTINGS: Dict[str, Any] = {
    "only_profitable": False,
    "profit_margin_pct": 5.0,
    "min_profit_rub": 0.0,
    "skip_no_price_data": True,
}

_cache: Dict[str, Any] = {}
_cache_mtime: float = 0.0


def _read_raw() -> Dict[str, Any]:
    if not os.path.exists(SETTINGS_FILE):
        return {}
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        # Битый файл не стираем — откладываем, чтобы ничего не потерять
        try:
            shutil.copy2(SETTINGS_FILE, SETTINGS_FILE + ".corrupt")
        except Exception:
            pass
        return {}


def load_settings(force: bool = False) -> Dict[str, Any]:
    """Возвращает настройки (с дефолтами) и кэширует их."""
    global _cache, _cache_mtime
    try:
        mtime = os.path.getmtime(SETTINGS_FILE) if os.path.exists(SETTINGS_FILE) else 0.0
    except OSError:
        mtime = 0.0

    if not force and _cache and mtime == _cache_mtime:
        return dict(_cache)

    data = _read_raw()
    merged = dict(DEFAULT_SETTINGS)
    for key in DEFAULT_SETTINGS:
        if key in data:
            merged[key] = data[key]
    _cache = merged
    _cache_mtime = mtime
    return dict(merged)


def save_settings(values: Dict[str, Any]) -> Dict[str, Any]:
    """Сохраняет настройки атомарно (tmp + replace) и обновляет кэш."""
    global _cache, _cache_mtime
    current = load_settings(force=True)
    current.update({k: v for k, v in values.items() if k in DEFAULT_SETTINGS})

    tmp = SETTINGS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(current, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SETTINGS_FILE)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return dict(current)

    _cache = dict(current)
    try:
        _cache_mtime = os.path.getmtime(SETTINGS_FILE)
    except OSError:
        _cache_mtime = 0.0
    return dict(current)


def get(key: str, default: Any = None) -> Any:
    data = load_settings()
    if key in data:
        return data[key]
    return DEFAULT_SETTINGS.get(key, default)


def set_value(key: str, value: Any) -> Dict[str, Any]:
    return save_settings({key: value})


# ---------------------------------------------------------------------------
# Расчёт выгоды
# ---------------------------------------------------------------------------


def profit_check(price_rub: float, market_rub: float, fee_rub: float = 0.0) -> Dict[str, float]:
    """Считает выгоду покупки.

    price_rub  — цена лота на FunPay/PlayerOK, ₽
    market_rub — рыночная цена (средняя по истории или floor), ₽
    fee_rub    — комиссия сети/перевода, ₽

    Возвращает: gross (в плюс без комиссии), net (с комиссией),
    need (сколько нужно заработать), profitable (в плюс или нет).
    """
    settings = load_settings()
    gross = float(market_rub) - float(price_rub)
    net = gross - float(fee_rub or 0.0)
    margin_pct = float(settings.get("profit_margin_pct", 0) or 0)
    min_profit = float(settings.get("min_profit_rub", 0) or 0)
    need = max(min_profit, float(market_rub) * margin_pct / 100.0)
    return {
        "gross": gross,
        "net": net,
        "need": need,
        "profitable": bool(net > 0 and net >= need),
    }


def only_profitable_enabled() -> bool:
    return bool(get("only_profitable", False))


def skip_no_price_data_enabled() -> bool:
    """Не присылать лоты, по которым не нашлись данные о ценах."""
    return bool(get("skip_no_price_data", True))


def deferred_send_enabled() -> bool:
    """Нужна ли отложенная отправка уведомлений.

    True — когда активен фильтр, способный отклонить лот («только выгодные»
    или «пропускать без данных о ценах»). В этом режиме бот вообще не
    отправляет лот в чат, пока не посчитает цены и профит, а отправляет
    только прошедшие фильтры. Так в чате не мелькают предложения, которые
    пришлось бы удалять через несколько секунд.
    """
    if only_profitable_enabled():
        return True
    if skip_no_price_data_enabled():
        return True
    return False


def should_notify(
    price_rub: float,
    market_rub: float,
    fee_rub: float = 0.0,
    certain: bool = False,
) -> Tuple[bool, str]:
    """Решает, показывать ли лот пользователю.

    price_rub  — цена лота, ₽
    market_rub — рыночная цена, ₽ (0 — цены не нашлись)
    fee_rub    — комиссия, ₽
    certain    — рыночная цена посчитана по реальным продажам
                 (а не по минимальной цене в продаже)

    Возвращает (показывать, причина отказа).
    """
    if market_rub <= 0:
        if skip_no_price_data_enabled():
            return False, "данные о ценах не найдены"
        return True, ""

    if not only_profitable_enabled():
        return True, ""

    check = profit_check(price_rub, market_rub, fee_rub)
    if not certain:
        return False, "рыночная цена не по реальным продажам"
    if not check["profitable"]:
        return False, f"профит {check['net']:.0f} ₽ < нужно {check['need']:.0f} ₽"
    return True, ""


def describe() -> str:
    """Человекочитаемое описание настроек."""
    s = load_settings()
    on = "включено" if s["only_profitable"] else "выключено"
    skip = "включено" if s.get("skip_no_price_data") else "выключено"
    return (
        f"Только выгодные предложения: <b>{on}</b>\n"
        f"Минимальная маржа: <b>{float(s['profit_margin_pct']):g}%</b>\n"
        f"Минимальная прибыль: <b>{float(s['min_profit_rub']):g} ₽</b>\n"
        f"Пропускать без данных о ценах: <b>{skip}</b>"
    )
