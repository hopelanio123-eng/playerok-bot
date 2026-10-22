# -*- coding: utf-8 -*-
"""
Общие команды управления для ботов проекта playerok-bot.

Подключение в любом боте проекта (сразу после создания dp/bot, ДО остальных
обработчиков, иначе перехватят общие текстовые хендлеры):

    from playerok_bot.control.bot_control import register_control
    register_control(dp, bot)

Команды:
    /mode            - текущий режим + кнопки переключения
    /mode <режим>    - переключить режим
    /bots            - состояние всех компонентов проекта

Управление идёт через локальный контроллер `playerok_bot.controller`
(порт 9324), который поднимает/останавливает процессы ботов и серверов.
"""

import os
import logging
from typing import Iterable, Optional, Set

import httpx
from aiogram import F, types
from aiogram.filters import Command, CommandObject
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from playerok_bot.control import app_settings
from playerok_bot.ai import ai_providers
from playerok_bot.ai._dotenv import load_env

load_env()

logger = logging.getLogger("bot_control")

CONTROLLER_URL = os.getenv("CONTROLLER_URL", "http://127.0.0.1:9324").rstrip("/")
STATUS_TIMEOUT = 20.0
SWITCH_TIMEOUT = 90.0

# Владельцы бота задаются через CONTROL_ALLOWED_USERS (id через запятую).
DEFAULT_ALLOWED_USERS: Set[int] = set()

# (ключ режима, подпись на кнопке)
MODES = [
    ("all", "PlayerOK"),
    ("playerok", "PlayerOK (botPl)"),
    ("off", "Остановить бота"),
]


def _parse_allowed(raw: Optional[str]) -> Set[int]:
    out: Set[int] = set()
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return out


def allowed_users() -> Set[int]:
    """Кому разрешено переключать режимы (CONTROL_ALLOWED_USERS в окружении)."""
    env = _parse_allowed(os.getenv("CONTROL_ALLOWED_USERS", ""))
    return env or set(DEFAULT_ALLOWED_USERS)


def allowed_users_from_env() -> Set[int]:
    return allowed_users()


def _mode_title(mode: str) -> str:
    for key, title in MODES:
        if key == mode:
            return title
    return mode


def _is_allowed(event: types.Message) -> bool:
    user = event.from_user
    if user is None:
        return False
    allowed = allowed_users()
    return user.id in allowed or (user.id in allowed and user.is_bot is False)


async def _controller_status() -> Optional[dict]:
    try:
        async with httpx.AsyncClient(timeout=STATUS_TIMEOUT) as client:
            resp = await client.get(f"{CONTROLLER_URL}/api/status")
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        logger.warning("Controller status failed: %s", exc)
        return None


async def _controller_set_mode(mode: str) -> Optional[dict]:
    try:
        async with httpx.AsyncClient(timeout=SWITCH_TIMEOUT) as client:
            resp = await client.post(f"{CONTROLLER_URL}/api/mode", json={"mode": mode})
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        logger.warning("Controller mode switch failed: %s", exc)
        return None


def _format_status(data: dict) -> str:
    lines = ["<b>Состояние проекта</b>", ""]

    mode = data.get("mode", "?")
    lines.append(f"Режим: <b>{_mode_title(mode)}</b>")
    lines.append("")

    components = data.get("components", {}) or {}
    for name, info in components.items():
        icon = "🟢" if info.get("running") else "🔴"
        port = info.get("port")
        port_txt = f" :{port}" if port else ""
        title = info.get("title", name)
        lines.append(f"{icon} <code>{name}</code>{port_txt} — {title}")

    stopped = [n for n, i in components.items() if not i.get("running")]
    if stopped:
        lines.append("")
        lines.append(f"Остановлено: {len(stopped)}")
    return "\n".join(lines)


def _mode_keyboard(current: str) -> InlineKeyboardMarkup:
    rows = []
    for key, title in MODES:
        mark = "✅ " if key == current else ""
        rows.append([InlineKeyboardButton(text=f"{mark}{title}", callback_data=f"mode:{key}")])
    rows.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="mode:refresh")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _settings_keyboard() -> InlineKeyboardMarkup:
    s = app_settings.load_settings()
    on = bool(s.get("only_profitable"))
    toggle_text = "✅ Только выгодные — ВКЛ" if on else "⬜ Только выгодные — ВЫКЛ"
    skip = bool(s.get("skip_no_price_data", True))
    skip_text = (
        "✅ Пропускать без цен — ВКЛ" if skip else "⬜ Пропускать без цен — ВЫКЛ"
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=toggle_text, callback_data="set:only_profitable")],
            [InlineKeyboardButton(text=skip_text, callback_data="set:skip_no_price_data")],
            [
                InlineKeyboardButton(text="Маржа −1%", callback_data="set:margin:-1"),
                InlineKeyboardButton(
                    text=f"Маржа: {float(s.get('profit_margin_pct', 0)):g}%",
                    callback_data="set:margin_info",
                ),
                InlineKeyboardButton(text="Маржа +1%", callback_data="set:margin:1"),
            ],
            [
                InlineKeyboardButton(text="Мин. профит −100₽", callback_data="set:minprofit:-100"),
                InlineKeyboardButton(
                    text=f"Профит: {float(s.get('min_profit_rub', 0)):g}₽",
                    callback_data="set:minprofit_info",
                ),
                InlineKeyboardButton(text="Мин. профит +100₽", callback_data="set:minprofit:100"),
            ],
        ]
    )


def _ai_keyboard() -> InlineKeyboardMarkup:
    order = ai_providers.provider_order()
    rows = []
    for idx, name in enumerate(order):
        title = ai_providers.PROVIDER_TITLES.get(name, name)
        up = "⬆️" if idx > 0 else "  "
        down = "⬇️" if idx < len(order) - 1 else "  "
        rows.append(
            [
                InlineKeyboardButton(text=f"{up}", callback_data=f"ai:up:{name}"),
                InlineKeyboardButton(text=title, callback_data="ai:noop"),
                InlineKeyboardButton(text=f"{down}", callback_data=f"ai:down:{name}"),
            ]
        )
    rows.append([InlineKeyboardButton(text="🧪 Проверить", callback_data="ai:test")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _ai_status_text() -> str:
    try:
        status = await ai_providers.provider_status()
    except Exception as exc:
        return f"❌ Не удалось получить статус ИИ: {exc}"

    order = ai_providers.provider_order()
    lines = ["🧠 <b>Провайдеры ИИ</b>", ""]
    lines.append("Порядок: " + " → ".join(f"<code>{p}</code>" for p in order))
    lines.append("")
    for st in status:
        icon = "🟢" if st["available"] else "🔴"
        in_order = st["name"] in order
        mark = " " if in_order else " (не в цепочке)"
        lines.append(f"{icon} <b>{st['title']}</b>{mark}\n    <i>{st['detail']}</i>")
    lines.append("")
    lines.append(
        "<i>Кнопки ▲▼ меняют приоритет. Если у провайдера закончилась квота - "
        "бот сам переключится на следующего.</i>"
    )
    return "\n".join(lines)


def _apply_setting(action: str) -> str:
    s = app_settings.load_settings()

    if action == "only_profitable":
        new = not bool(s.get("only_profitable"))
        app_settings.set_value("only_profitable", new)
        return (
            "✅ Режим «только выгодные предложения» "
            + ("включён" if new else "выключен")
        )

    if action == "skip_no_price_data":
        new = not bool(s.get("skip_no_price_data", True))
        app_settings.set_value("skip_no_price_data", new)
        return (
            "✅ Пропуск лотов без данных о ценах "
            + ("включён" if new else "выключен")
        )

    if action.startswith("margin:"):
        try:
            delta = float(action.split(":", 1)[1])
        except ValueError:
            return "❌ Некорректное значение"
        value = round(float(s.get("profit_margin_pct", 0)) + delta, 2)
        value = max(0.0, min(value, 500.0))
        app_settings.set_value("profit_margin_pct", value)
        return f"✅ Минимальная маржа: <b>{value:g}%</b>"

    if action.startswith("minprofit:"):
        try:
            delta = float(action.split(":", 1)[1])
        except ValueError:
            return "❌ Некорректное значение"
        value = round(float(s.get("min_profit_rub", 0)) + delta, 2)
        value = max(0.0, min(value, 10_000_000.0))
        app_settings.set_value("min_profit_rub", value)
        return f"✅ Минимальная прибыль: <b>{value:g} ₽</b>"

    return "❌ Неизвестное действие"


async def check_group_access(bot, chat_id: int) -> Optional[str]:
    """Проверяет, что бот может писать в чат.

    Возвращает текст предупреждения, если доступа нет, иначе None.
    """
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(chat_id, me.id)
        if member.status in ("member", "administrator", "creator"):
            return None
        return (
            f"⚠️ Бот @{me.username} не состоит в чате {chat_id} (статус: {member.status}). "
            "Добавь бота в группу и выдай права на отправку сообщений."
        )
    except Exception as exc:
        return (
            f"⚠️ Не удалось проверить доступ к чату {chat_id}: {exc}. "
            "Добавь бота в группу вручную и выдай права администратора."
        )


def register_conflict_hint(dp=None) -> None:
    """Понятная подсказка вместо сырого TelegramConflictError.

    aiogram логирует конфликт getUpdates сам (логгер aiogram.dispatcher),
    поэтому ловим его фильтром и дописываем объяснение.
    """
    import logging

    aiogram_logger = logging.getLogger("aiogram.dispatcher")
    if getattr(aiogram_logger, "_tg_conflict_hint", False):
        return

    class _Hint(logging.Filter):
        def filter(self, record):
            try:
                msg = record.getMessage()
            except Exception:
                return True
            if "TelegramConflictError" in msg or "terminated by other getUpdates" in msg:
                if not getattr(aiogram_logger, "_tg_conflict_hinted", False):
                    aiogram_logger._tg_conflict_hinted = True
                    logger.error(
                        "⚠️ Конфликт getUpdates: этот же бот уже запущен в другом месте "
                        "(Termux / VPS / другая копия проекта). Останови там — иначе оба "
                        "бота будут драться за апдейты и слать дубли."
                    )
            return True

    aiogram_logger.addFilter(_Hint())
    aiogram_logger._tg_conflict_hint = True


def register_control(dp, bot, allowed_users: Optional[Iterable[int]] = None) -> None:
    """Регистрирует /mode, /mode <режим> и /bots в переданном диспетчере."""
    allow_set: Set[int] = set(allowed_users) if allowed_users else allowed_users_from_env()
    if not allow_set:
        logger.warning(
            "CONTROL_ALLOWED_USERS is not set - control commands (/mode, /settings, /ai) are disabled"
        )
    register_conflict_hint(dp)

    def _check(event: types.Message) -> bool:
        user = event.from_user
        if user is None:
            return False
        return user.id in allow_set

    @dp.message(Command("mode"))
    async def cmd_mode(message: types.Message, command: CommandObject = None) -> None:
        if not _check(message):
            await message.answer("⛔ Нет доступа.")
            return

        arg = (command.args if command else "") or ""
        arg = arg.strip().lower()

        if arg:
            valid = {k for k, _ in MODES}
            if arg not in valid:
                await message.answer(
                    f"❓ Неизвестный режим: <code>{arg}</code>\n"
                    "Доступные: " + ", ".join(f"<code>{k}</code>" for k, _ in MODES)
                )
                return

            result = await _controller_set_mode(arg)
            if result is None:
                await message.answer(
                    "❌ Контроллер не отвечает.\n"
                    "Запусти его: <code>start.bat</code> в папке проекта."
                )
                return

            ok = result.get("ok", False)
            text = _format_status(result)
            if not ok:
                text = f"⚠️ {result.get('error', 'ошибка')}\n\n" + text
            await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)
            return

        data = await _controller_status()
        if data is None:
            await message.answer(
                "❌ Контроллер не отвечает.\n"
                "Запусти его: <code>start.bat</code> в папке проекта."
            )
            return

        await message.answer(
            _format_status(data),
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=_mode_keyboard(data.get("mode", "off")),
        )

    @dp.message(Command("bots"))
    async def cmd_bots(message: types.Message) -> None:
        if not _check(message):
            await message.answer("⛔ Нет доступа.")
            return

        data = await _controller_status()
        if data is None:
            await message.answer(
                "❌ Контроллер не отвечает.\n"
                "Запусти его: <code>start.bat</code> в папке проекта."
            )
            return

        await message.answer(
            _format_status(data),
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=_mode_keyboard(data.get("mode", "off")),
        )

    # ---------------- провайдеры ИИ ----------------

    @dp.message(Command("ai"))
    async def cmd_ai(message: types.Message, command: CommandObject = None) -> None:
        if not _check(message):
            await message.answer("⛔ Нет доступа.")
            return

        arg = ((command.args if command else "") or "").strip().lower()
        if arg:
            wanted = [p for p in arg.replace(";", ",").split(",") if p.strip()]
            if all(p in ai_providers.VALID for p in wanted) and wanted:
                saved = ai_providers.save_order(wanted)
                await message.answer(
                    "✅ Порядок провайдеров ИИ: "
                    + " → ".join(f"<code>{p}</code>" for p in saved),
                    parse_mode="HTML",
                )
                return

        await message.answer(
            await _ai_status_text(),
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=_ai_keyboard(),
        )

    @dp.callback_query(F.data.startswith("ai:"))
    async def cb_ai(call: types.CallbackQuery) -> None:
        user = call.from_user
        if user is None or user.id not in allow_set:
            await call.answer("⛔ Нет доступа.", show_alert=True)
            return

        parts = (call.data or "").split(":")
        action = parts[1] if len(parts) > 1 else ""

        if action == "noop":
            await call.answer()
            return

        if action == "test":
            await call.answer("Проверяю…")
            from playerok_bot.ai import airequests

            res, provider = await ai_providers.call_ai(
                "Ты аналитик подарков. Отвечай строго по схеме.",
                "Напиши в json: {\"collection\":\"test\"}",
                {
                    "type": "object",
                    "properties": {"collection": {"type": "string", "description": "имя"}},
                    "required": ["collection"],
                },
                kind="chat",
            )
            text = (
                f"✅ Ответил провайдер: <code>{provider}</code>\n{res}"
                if provider
                else "❌ Ни один провайдер не ответил"
            )
            await call.message.answer(text, parse_mode="HTML")
            return

        if action in ("up", "down") and len(parts) > 2:
            name = parts[2]
            order = ai_providers.provider_order()
            if name not in order:
                await call.answer("Провайдер не найден", show_alert=True)
                return
            order.remove(name)
            idx = order.index(name) if name in order else len(order)
            if action == "up":
                idx = max(0, idx - 1)
            else:
                idx = min(len(order), idx + 1)
            order.insert(idx, name)
            ai_providers.save_order(order)
            await call.answer("Порядок обновлён")
            try:
                await call.message.edit_text(
                    await _ai_status_text(),
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                    reply_markup=_ai_keyboard(),
                )
            except Exception:
                pass
            return

        await call.answer()

    # ---------------- настройки "только выгодные предложения" ----------------

    @dp.message(Command("settings"))
    async def cmd_settings(message: types.Message) -> None:
        if not _check(message):
            await message.answer("\u26d4 \u041d\u0435\u0442 \u0434\u043e\u0441\u0442\u0443\u043f\u0430.")
            return

        await message.answer(
            "\u2699\ufe0f <b>\u041d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0438</b>\n\n"
            + app_settings.describe()
            + "\n\n<i>\u041a\u043e\u0433\u0434\u0430 \u0432\u043a\u043b\u044e\u0447\u0435\u043d\u043e - \u0431\u043e\u0442 \u043f\u0440\u0438\u0441\u044b\u043b\u0430\u0435\u0442 \u0442\u043e\u043b\u044c\u043a\u043e \u0442\u0435, \u043a\u043e\u0442\u043e\u0440\u044b\u0435 \u043c\u043e\u0436\u043d\u043e "
              "\u043a\u0443\u043f\u0438\u0442\u044c \u0432\u044b\u0433\u043e\u0434\u043d\u043e (\u043d\u0438\u0436\u0435 \u0440\u044b\u043d\u043a\u0430), \u0438 \u043d\u0435 \u043e\u0442\u043f\u0440\u0430\u0432\u043b\u044f\u0435\u0442 "
              "\u0437\u0430\u0432\u0435\u0434\u043e\u043c\u043e \u0443\u0431\u044b\u0442\u043e\u0447\u043d\u044b\u0435 \u043e\u0444\u0444\u0435\u0440\u044b.</i>",
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=_settings_keyboard(),
        )

    @dp.message(Command("profitable"))
    async def cmd_profitable(message: types.Message, command: CommandObject = None) -> None:
        if not _check(message):
            await message.answer("\u26d4 \u041d\u0435\u0442 \u0434\u043e\u0441\u0442\u0443\u043f\u0430.")
            return

        arg = ((command.args if command else "") or "").strip().lower()
        if arg in ("on", "\u0432\u043a\u043b", "\u0434\u0430", "1", "true"):
            app_settings.set_value("only_profitable", True)
            text = "\u2705 \u0420\u0435\u0436\u0438\u043c \u00ab\u0442\u043e\u043b\u044c\u043a\u043e \u0432\u044b\u0433\u043e\u0434\u043d\u044b\u0435 \u043f\u0440\u0435\u0434\u043b\u043e\u0436\u0435\u043d\u0438\u044f\u00bb \u0432\u043a\u043b\u044e\u0447\u0451\u043d"
        elif arg in ("off", "\u0432\u044b\u043a\u043b", "\u043d\u0435\u0442", "0", "false"):
            app_settings.set_value("only_profitable", False)
            text = "\u2705 \u0420\u0435\u0436\u0438\u043c \u00ab\u0442\u043e\u043b\u044c\u043a\u043e \u0432\u044b\u0433\u043e\u0434\u043d\u044b\u0435 \u043f\u0440\u0435\u0434\u043b\u043e\u0436\u0435\u043d\u0438\u044f\u00bb \u0432\u044b\u043a\u043b\u044e\u0447\u0451\u043d"
        else:
            text = (
                "\u2139\ufe0f \u0422\u0435\u043a\u0443\u0449\u0435\u0435 \u0441\u043e\u0441\u0442\u043e\u044f\u043d\u0438\u0435:\n" + app_settings.describe()
                + "\n\n\u0412\u043a\u043b\u044e\u0447\u0438\u0442\u044c: <code>/profitable on</code> \u00b7 \u0412\u044b\u043a\u043b\u044e\u0447\u0438\u0442\u044c: <code>/profitable off</code>"
            )
        await message.answer(text, parse_mode="HTML", reply_markup=_settings_keyboard())

    @dp.message(Command("margin"))
    async def cmd_margin(message: types.Message, command: CommandObject = None) -> None:
        if not _check(message):
            await message.answer("\u26d4 \u041d\u0435\u0442 \u0434\u043e\u0441\u0442\u0443\u043f\u0430.")
            return

        arg = ((command.args if command else "") or "").strip().replace(",", ".")
        if not arg:
            await message.answer(
                "\u2139\ufe0f \u041c\u0430\u0440\u0436\u0430: <b>{:g}%</b>\n\u0417\u0430\u0434\u0430\u0442\u044c: <code>/margin 10</code>".format(
                    float(app_settings.get("profit_margin_pct", 0))
                ),
                parse_mode="HTML",
            )
            return
        try:
            value = max(0.0, min(float(arg), 500.0))
        except ValueError:
            await message.answer("\u274c \u041d\u0443\u0436\u043d\u043e \u0447\u0438\u0441\u043b\u043e, \u043d\u0430\u043f\u0440\u0438\u043c\u0435\u0440: <code>/margin 10</code>")
            return
        app_settings.set_value("profit_margin_pct", value)
        await message.answer(
            "\u2705 \u041c\u0438\u043d\u0438\u043c\u0430\u043b\u044c\u043d\u0430\u044f \u043c\u0430\u0440\u0436\u0430 \u0432\u044b\u0433\u043e\u0434\u044b: <b>{:g}%</b>".format(value),
            parse_mode="HTML",
            reply_markup=_settings_keyboard(),
        )

    @dp.message(Command("minprofit"))
    async def cmd_minprofit(message: types.Message, command: CommandObject = None) -> None:
        if not _check(message):
            await message.answer("\u26d4 \u041d\u0435\u0442 \u0434\u043e\u0441\u0442\u0443\u043f\u0430.")
            return

        arg = ((command.args if command else "") or "").strip().replace(",", ".")
        if not arg:
            await message.answer(
                "\u2139\ufe0f \u041c\u0438\u043d\u0438\u043c\u0430\u043b\u044c\u043d\u0430\u044f \u043f\u0440\u0438\u0431\u044b\u043b\u044c: <b>{:g} \u20bd</b>\n\u0417\u0430\u0434\u0430\u0442\u044c: <code>/minprofit 500</code>".format(
                    float(app_settings.get("min_profit_rub", 0))
                ),
                parse_mode="HTML",
            )
            return
        try:
            value = max(0.0, min(float(arg), 10_000_000.0))
        except ValueError:
            await message.answer("\u274c \u041d\u0443\u0436\u043d\u043e \u0447\u0438\u0441\u043b\u043e, \u043d\u0430\u043f\u0440\u0438\u043c\u0435\u0440: <code>/minprofit 500</code>")
            return
        app_settings.set_value("min_profit_rub", value)
        await message.answer(
            "\u2705 \u041c\u0438\u043d\u0438\u043c\u0430\u043b\u044c\u043d\u0430\u044f \u043f\u0440\u0438\u0431\u044b\u043b\u044c: <b>{:g} \u20bd</b>".format(value),
            parse_mode="HTML",
            reply_markup=_settings_keyboard(),
        )

    @dp.callback_query(F.data.startswith("set:"))
    async def cb_settings(call: types.CallbackQuery) -> None:
        user = call.from_user
        if user is None or user.id not in allow_set:
            await call.answer("\u26d4 \u041d\u0435\u0442 \u0434\u043e\u0441\u0442\u0443\u043f\u0430.", show_alert=True)
            return

        action = (call.data or "")[4:]
        if action.endswith("_info"):
            await call.answer("\u2139\ufe0f " + app_settings.describe().replace("<b>", "").replace("</b>", ""))
            return

        result = _apply_setting(action)
        await call.answer(result.replace("<b>", "").replace("</b>", ""))
        try:
            await call.message.edit_text(
                "\u2699\ufe0f <b>\u041d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0438</b>\n\n"
                + app_settings.describe(),
                parse_mode="HTML",
                reply_markup=_settings_keyboard(),
            )
        except Exception:
            pass

    @dp.callback_query(F.data.startswith("mode:"))
    async def cb_mode(call: types.CallbackQuery) -> None:
        user = call.from_user
        if user is None or user.id not in allow_set:
            await call.answer("⛔ Нет доступа.", show_alert=True)
            return

        action = (call.data or "").split(":", 1)[1].strip().lower()

        if action == "refresh":
            data = await _controller_status()
            if data is None:
                await call.answer("Контроллер недоступен", show_alert=True)
                return
            await call.message.edit_text(
                _format_status(data),
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=_mode_keyboard(data.get("mode", "off")),
            )
            await call.answer()
            return

        valid = {k for k, _ in MODES}
        if action not in valid:
            await call.answer("❓ Неизвестный режим", show_alert=True)
            return

        await call.answer(f"Переключаю: {_mode_title(action)}...")
        result = await _controller_set_mode(action)
        if result is None:
            await call.message.answer(
                "❌ Контроллер не отвечает.\n"
                "Запусти его: <code>start.bat</code> в папке проекта."
            )
            return

        text = _format_status(result)
        if not result.get("ok", False):
            text = f"⚠️ {result.get('error', 'ошибка')}\n\n" + text

        await call.message.edit_text(
            text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=_mode_keyboard(result.get("mode", action)),
        )
