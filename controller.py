# -*- coding: utf-8 -*-
"""
controller.py — единый супервизор проекта tg.

Поднимает и останавливает все процессы проекта, отдаёт статус и принимает
команду переключения режима (её и вызывают /mode и /bots в ботах).

Что поднимается:
  CORE (всегда):
    market_server.py  -> :9321  прокси к gift-satellite.dev (нужен боту)
    gemini_server.py  -> :9322  прокси к Gemini (нужен для анализа картинок)
    gstatesearch.py   -> :9323  инлайн-бот оценки подарков (свой токен)

  БОТЫ (переключаются через /mode):
    all       -> bot_smart.py + botPl.py
    funpay    -> bot_smart.py
    playerok  -> botPl.py
    watcher   -> start_gift_bot.py (gift_watcher, telethon)
    off       -> ничего

Важно: бот bot_smart.py и gift_watcher используют ОДИН и тот же токен
(6793575807 / @dianochkin_bot), поэтому одновременно они не запускаются —
Telegram отдаёт Conflict: terminated by other getUpdates request.
"""

import asyncio
import ctypes
import json
import os
import signal
import subprocess
import sys
import time
from typing import Dict, List, Optional

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

from _dotenv import load_env

load_env()
CONTROL_PORT = int(os.getenv("CONTROL_PORT", "9324"))
STATE_FILE = os.path.join(BASE_DIR, "control_state.json")
PID_FILE = os.path.join(BASE_DIR, "controller.pid")

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
else:
    # Запуск через pythonw.exe — потоков нет. Подставляем пустые, чтобы
    # uvicorn и logging не падали на None.
    try:
        import io

        sys.stdout = io.StringIO()
        sys.stderr = io.StringIO()
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Описание компонентов
# ---------------------------------------------------------------------------

CORE: List[dict] = [
    {
        "name": "market_server",
        "script": "market_server.py",
        "title": "Прокси Gift Satellite API",
        "port": 9321,
        "health": "http://127.0.0.1:9321/health",
    },
    {
        "name": "gemini_server",
        "script": "gemini_server.py",
        "title": "Прокси Gemini (анализ фото)",
        "port": 9322,
        "health": "http://127.0.0.1:9322/health",
    },
]

BOTS: List[dict] = [
    {
        "name": "botPl",
        "script": "botPl.py",
        "title": "PlayerOK трейдинг-бот",
        "port": None,
        "health": None,
    },
]

MODES: Dict[str, List[str]] = {
    "all": ["botPl"],
    "playerok": ["botPl"],
    "off": [],
}

MODE_TITLES = {
    "all": "PlayerOK",
    "playerok": "PlayerOK (botPl)",
    "off": "Остановить бота",
}

ALL_COMPONENTS = {c["name"]: c for c in (CORE + BOTS)}

# ---------------------------------------------------------------------------
# Состояние
# ---------------------------------------------------------------------------

procs: Dict[str, subprocess.Popen] = {}
current_mode: str = "all"
lock = asyncio.Lock()


def log(msg: str) -> None:
    print(f"[CTRL {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def save_state() -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump({"mode": current_mode}, fh, indent=2, ensure_ascii=False)
    except Exception as exc:
        log(f"Не удалось сохранить состояние: {exc}")


def load_state() -> str:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        mode = data.get("mode", "all")
        return mode if mode in MODES else "all"
    except Exception:
        return "all"


# ---------------------------------------------------------------------------
# Запуск / остановка процессов
# ---------------------------------------------------------------------------

CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
# Окна консоли от сервисов и ботов не нужны — логи идут в stdout/pipe.
CHILD_FLAGS = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW if os.name == "nt" else 0


def child_env() -> dict:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONUTF8"] = "1"
    return env


def child_streams():
    """Потоки для дочерних процессов.

    При CREATE_NO_WINDOW дочерние процессы НЕ наследуют вывод родителя,
    поэтому передаём их явно - иначе логи сервисов и ботов теряются.
    """
    out, err = sys.stdout, sys.stderr
    if out is None or err is None:
        # Контроллер запущен без консоли (pythonw) - пишем в файл
        try:
            fallback = open(os.path.join(BASE_DIR, "controller.log"), "a", encoding="utf-8")
            return fallback, fallback
        except Exception:
            return None, None
    return out, err


def start_component(spec: dict) -> bool:
    name = spec["name"]
    if name in procs and procs[name].poll() is None:
        return True

    script = os.path.join(BASE_DIR, spec["script"])
    if not os.path.exists(script):
        log(f"✗ {name}: нет файла {spec['script']}")
        return False

    flags = CHILD_FLAGS
    try:
        child_out, child_err = child_streams()
        proc = subprocess.Popen(
            [sys.executable, "-u", script],
            cwd=BASE_DIR,
            env=child_env(),
            creationflags=flags,
            stdout=child_out,
            stderr=child_err,
        )
    except Exception as exc:
        log(f"✗ {name}: не удалось запустить ({exc})")
        return False

    procs[name] = proc
    log(f"▶ {name} запущен (pid {proc.pid}) — {spec['title']}")
    return True


async def stop_component(name: str, timeout: float = 6.0) -> None:
    proc = procs.get(name)
    if not proc or proc.poll() is not None:
        procs.pop(name, None)
        return

    log(f"⏹ {name}: останавливаю (pid {proc.pid})…")

    # 1) Мягкая остановка. CTRL_BREAK работает только если процесс и контроллер
    #    в одной консоли; у оконных процессов (CREATE_NO_WINDOW) консоли нет,
    #    поэтому дальше обязательно идёт жёсткое завершение.
    try:
        if os.name == "nt":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.terminate()
    except Exception:
        pass

    soft_deadline = time.time() + max(1.5, timeout / 2)
    while time.time() < soft_deadline:
        if proc.poll() is not None:
            log(f"✓ {name}: остановлен")
            procs.pop(name, None)
            return
        await asyncio.sleep(0.2)

    # 2) TerminateProcess — работает всегда
    try:
        proc.terminate()
    except Exception:
        pass

    hard_deadline = time.time() + 2.0
    while time.time() < hard_deadline:
        if proc.poll() is not None:
            log(f"✓ {name}: остановлен")
            procs.pop(name, None)
            return
        await asyncio.sleep(0.2)

    # 3) Последний вариант
    log(f"✗ {name}: не ответил, убиваю принудительно")
    try:
        proc.kill()
    except Exception:
        pass
    procs.pop(name, None)


async def stop_all_components(timeout: float = 6.0) -> None:
    """Останавливает все дочерние процессы параллельно (быстро)."""
    names = list(procs.keys())
    if not names:
        return
    await asyncio.gather(
        *[stop_component(n, timeout) for n in names],
        return_exceptions=True,
    )


async def wait_health(url: str, timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    async with httpx.AsyncClient(timeout=5.0) as client:
        while time.time() < deadline:
            try:
                resp = await client.get(url)
                if resp.status_code == 200:
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.7)
    return False


async def ensure_core() -> None:
    for spec in CORE:
        ok = start_component(spec)
        if not ok:
            continue
        if spec.get("health"):
            ready = await wait_health(spec["health"], timeout=40.0)
            if ready:
                log(f"✓ {spec['name']}: готов ({spec.get('port')})")
            else:
                log(f"⚠ {spec['name']}: не отвечает по {spec['health']} (продолжаю)")
        else:
            await asyncio.sleep(0.5)


async def apply_mode(mode: str) -> str:
    """Переключает набор ботов. Возвращает текст результата."""
    global current_mode

    if mode not in MODES:
        return f"Неизвестный режим: {mode}"

    wanted = set(MODES[mode])
    current = set(procs.keys()) & {c["name"] for c in BOTS}

    for name in current - wanted:
        await stop_component(name)

    for spec in BOTS:
        if spec["name"] in wanted:
            if spec["name"] not in procs or procs[spec["name"]].poll() is not None:
                # Ботам нужны прокси — дождаться их готовности.
                await ensure_core()
                start_component(spec)
                await asyncio.sleep(3.0)

    current_mode = mode
    save_state()
    return f"Режим переключён: {MODE_TITLES.get(mode, mode)}"


# ---------------------------------------------------------------------------
# Уборка «осиротевших» процессов от прошлого запуска
# ---------------------------------------------------------------------------

ORPHAN_SCRIPTS = [
    "market_server.py",
    "gemini_server.py",
    "botPl.py",
]


def kill_orphans() -> None:
    """Гасит python-процессы этого проекта, оставшиеся от прошлого запуска."""
    if os.name != "nt":
        return
    pattern = "|".join(s.replace(".", r"\.") for s in ORPHAN_SCRIPTS)
    ps = (
        "$ErrorActionPreference='SilentlyContinue';"
        f"$pat='{pattern}';"
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe'\" | "
        "Where-Object { $_.CommandLine -and $_.CommandLine -match $pat } | "
        "ForEach-Object { Write-Output $_.ProcessId }"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            timeout=40,
        )
        raw = (out.stdout or b"").decode("utf-8", errors="replace")
        pids = [p.strip() for p in raw.split() if p.strip().isdigit()]
        for pid in pids:
            if int(pid) == os.getpid():
                continue
            log(f"✂ Гашу осиротевший процесс pid {pid}")
            subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True, timeout=20)
    except Exception as exc:
        log(f"⚠ Не удалось прочистить осиротевшие процессы: {exc}")


def controller_alive() -> bool:
    try:
        with open(PID_FILE, "r", encoding="utf-8") as fh:
            pid = int(fh.read().strip())
    except Exception:
        return False
    if pid == os.getpid():
        return True
    return _pid_alive(pid)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

app = FastAPI(title="tg project controller", version="1.0.0")

# Событие остановки (по /api/shutdown — например, из трея)
shutdown_event = asyncio.Event()
server_ref = {"server": None}


class ModeRequest(BaseModel):
    mode: str


def status_payload() -> dict:
    components = {}
    for spec in CORE + BOTS:
        name = spec["name"]
        proc = procs.get(name)
        components[name] = {
            "title": spec["title"],
            "script": spec["script"],
            "port": spec.get("port"),
            "running": bool(proc and proc.poll() is None),
            "pid": proc.pid if proc and proc.poll() is None else None,
            "core": spec in CORE,
        }
    return {
        "ok": True,
        "mode": current_mode,
        "mode_title": MODE_TITLES.get(current_mode, current_mode),
        "components": components,
        "modes": [{"key": k, "title": v} for k, v in MODE_TITLES.items()],
    }


@app.get("/health")
async def health():
    return {"status": "ok", "mode": current_mode}


@app.get("/api/status")
async def api_status():
    return status_payload()


@app.post("/api/mode")
async def api_mode(req: ModeRequest):
    async with lock:
        if req.mode not in MODES:
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": f"Неизвестный режим: {req.mode}"},
            )
        try:
            message = await apply_mode(req.mode)
        except Exception as exc:
            return JSONResponse(
                status_code=500,
                content={"ok": False, "error": str(exc), **status_payload()},
            )
        payload = status_payload()
        payload["message"] = message
        return payload


@app.post("/api/shutdown")
async def api_shutdown():
    """Мягко останавливает контроллер и все дочерние процессы."""
    log("Получена команда остановки (через трей / API)")
    server = server_ref.get("server")
    if server is not None:
        server.should_exit = True
    shutdown_event.set()
    return {"ok": True, "message": "Останавливаюсь…"}


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


async def watch_parent() -> None:
    """Следит за родителем (трей-лаунчером): если он исчез — выключаемся.

    Включается переменной TRACK_PARENT=1 (её ставит tray_launcher.py),
    чтобы при запуске контроллера вручную он не выключался сам.
    """
    parent_pid = os.getppid()
    log(f"Слежу за родителем (pid {parent_pid})")
    while True:
        await asyncio.sleep(5)
        if not _pid_alive(parent_pid):
            log("Родительский процесс исчез — останавливаюсь")
            server = server_ref.get("server")
            if server is not None:
                server.should_exit = True
            shutdown_event.set()
            return


async def main() -> None:
    global current_mode

    try:
        import uvicorn
    except ImportError:
        print("✗ Нет uvicorn. Установи зависимости: pip install -r requirements.txt")
        sys.exit(1)

    if controller_alive() and _other_controller_running():
        print("✗ Контроллер уже запущен (см. controller.pid). Закрой его или удали controller.pid.")
        sys.exit(2)

    with open(PID_FILE, "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))

    log("Чищу осиротевшие процессы прошлого запуска…")
    kill_orphans()

    current_mode = load_state()
    log(f"Режим из сохранённого состояния: {MODE_TITLES.get(current_mode, current_mode)}")

    log("Поднимаю базовые сервисы…")
    await ensure_core()

    log("Поднимаю ботов…")
    async with lock:
        await apply_mode(current_mode)

    config = uvicorn.Config(app, host="127.0.0.1", port=CONTROL_PORT, log_level="warning")
    server = uvicorn.Server(config)
    server_ref["server"] = server

    async def serve() -> None:
        await server.serve()

    task = asyncio.create_task(serve())
    log(f"✅ Контроллер готов: http://127.0.0.1:{CONTROL_PORT}")
    log("Команды в Telegram: /mode  /mode " + "|".join(MODES.keys()) + "  /bots")

    watcher = asyncio.create_task(shutdown_event.wait())
    tasks = {task, watcher}
    if os.getenv("TRACK_PARENT", "").strip() in ("1", "true", "yes"):
        tasks.add(asyncio.create_task(watch_parent()))
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            if t is task and t.exception():
                raise t.exception()
    except asyncio.CancelledError:
        pass
    finally:
        watcher.cancel()
        log("Останавливаю всё…")
        await stop_all_components(timeout=6.0)
        try:
            os.remove(PID_FILE)
        except Exception:
            pass
        log("Готово.")


def _other_controller_running() -> bool:
    try:
        with open(PID_FILE, "r", encoding="utf-8") as fh:
            pid = int(fh.read().strip())
    except Exception:
        return False
    return pid != os.getpid() and _pid_alive(pid)


def _pid_alive(pid: int) -> bool:
    """Проверяет, жив ли процесс.

    Используем WinAPI, а не tasklist: tasklist при chcp 65001 отдаёт UTF-16,
    который ломает декодирование (и раньше watchdog считал живого родителя
    мёртвым).
    """
    if pid <= 0:
        return False

    if os.name == "nt":
        try:
            kernel32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return code.value == STILL_ACTIVE
                return False
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False

    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nОстановлено пользователем.")
