#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tray_launcher.py — запуск проекта с иконкой в трее (Windows).

Что делает:
  * поднимает контроллер playerok_bot.controller (а тот — все сервисы и ботов);
  * показывает иконку в трее (область уведомлений);
  * из меню трея можно посмотреть логи, статус, сменить режим, перезапустить;
  * пункт «Выход» корректно останавливает контроллер и всех ботов.

Запуск:  python -m playerok_bot.tray_launcher   (или двойной клик по start.bat)
"""

import os
import subprocess
import sys
import threading
import time
import queue
from datetime import datetime

from playerok_bot.paths import PROJECT_DIR as BASE_DIR

LOG_FILE = os.path.join(BASE_DIR, "tray.log")
CONTROLLER_URL = "http://127.0.0.1:9324"
CONTROLLER_MODULE = "playerok_bot.controller"

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def console_python() -> str:
    """Интерпретатор с работающим stdout/stderr.

    Трей-лаунчер сам может быть запущен через pythonw.exe (без консоли),
    но контроллер и боты внутри него обязаны иметь нормальные потоки —
    иначе uvicorn/логирование падают. Поэтому берём соседний python.exe.
    """
    exe = sys.executable or "python"
    if os.name == "nt" and exe.lower().endswith("pythonw.exe"):
        candidate = os.path.join(os.path.dirname(exe), "python.exe")
        if os.path.exists(candidate):
            return candidate
    return exe


# ---------------------------------------------------------------------------
# Контроллер как дочерний процесс
# ---------------------------------------------------------------------------


class ControllerProcess:
    """Запускает playerok_bot.controller и читает его вывод."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen = None
        self.lines: "queue.Queue[str]" = queue.Queue()
        self._lock = threading.Lock()
        self._reader: threading.Thread = None
        self.attached = False

    # -- запуск ------------------------------------------------------------
    def _controller_alive(self) -> bool:
        try:
            import httpx

            httpx.get(f"{CONTROLLER_URL}/health", timeout=3)
            return True
        except Exception:
            return False

    def start(self) -> bool:
        with self._lock:
            if self.proc and self.proc.poll() is None:
                return True

            # Контроллер уже поднят (например, вручную) — подключаемся к нему
            if self._controller_alive():
                self.attached = True
                log("ℹ Контроллер уже запущен — подключаюсь к нему")
                return True

            self.attached = False

            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUNBUFFERED"] = "1"
            env["PYTHONUTF8"] = "1"
            # Контроллер должен остановиться, если трей-лаунчер умрёт
            env["TRACK_PARENT"] = "1"

            creationflags = 0
            if os.name == "nt":
                # CREATE_NO_WINDOW — контроллер не должен открывать своё окно консоли
                creationflags = (
                    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
                    | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
                )

            try:
                self.proc = subprocess.Popen(
                    [console_python(), "-u", "-m", CONTROLLER_MODULE],
                    cwd=BASE_DIR,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    creationflags=creationflags,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                )
            except Exception as exc:
                log(f"✗ Не удалось запустить контроллер: {exc}")
                return False

            log(f"▶ Контроллер запущен (pid {self.proc.pid})")
            self._reader = threading.Thread(target=self._read_loop, daemon=True)
            self._reader.start()
            return True

    def _read_loop(self) -> None:
        try:
            for raw in self.proc.stdout:
                line = raw.rstrip("\n")
                if not line:
                    continue
                self.lines.put(line)
                try:
                    with open(LOG_FILE, "a", encoding="utf-8") as fh:
                        fh.write(line + "\n")
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            self.lines.put("__CONTROLLER_EXITED__")

    # -- управление --------------------------------------------------------
    def is_running(self) -> bool:
        if self.attached:
            return self._controller_alive()
        return bool(self.proc and self.proc.poll() is None)
    def stop(self) -> None:
        """Мягко останавливает контроллер (он сам погасит детей)."""
        if self.attached:
            log("⏹ Останавливаю контроллер…")
            try:
                import httpx

                httpx.post(f"{CONTROLLER_URL}/api/shutdown", timeout=10)
            except Exception as exc:
                log(f"⚠ Не удалось остановить контроллер: {exc}")
            self.attached = False
            return

        if not self.is_running():
            return
        log("⏹ Останавливаю контроллер…")
        try:
            import httpx

            httpx.post(f"{CONTROLLER_URL}/api/shutdown", timeout=10)
        except Exception:
            pass

        deadline = time.time() + 20
        while time.time() < deadline and self.is_running():
            time.sleep(0.3)

        if self.is_running():
            log("⚠ Контроллер не ответил — убиваю принудительно")
            try:
                self.proc.kill()
            except Exception:
                pass
        else:
            log("✓ Контроллер остановлен")

    def restart(self) -> None:
        self.stop()
        time.sleep(1.5)
        self.start()


# ---------------------------------------------------------------------------
# Иконка
# ---------------------------------------------------------------------------


def make_icon_image():
    """Рисуем иконку 64x64 через Pillow (никаких внешних файлов не нужно)."""
    from PIL import Image, ImageDraw

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # скруглённый синий квадрат
    d.rounded_rectangle([2, 2, size - 2, size - 2], radius=14, fill=(37, 99, 235, 255))
    # «глаза» антенны-бота
    d.ellipse([14, 12, 24, 22], fill=(255, 255, 255, 255))
    d.ellipse([40, 12, 50, 22], fill=(255, 255, 255, 255))
    d.ellipse([17, 15, 21, 19], fill=(15, 23, 42, 255))
    d.ellipse([43, 15, 47, 19], fill=(15, 23, 42, 255))
    # корпус
    d.rounded_rectangle([12, 28, size - 12, 50], radius=8, fill=(226, 232, 240, 255))
    d.rectangle([26, 34, 38, 38], fill=(37, 99, 235, 255))
    d.rectangle([26, 42, 38, 46], fill=(148, 163, 184, 255))
    return img


# ---------------------------------------------------------------------------
# Окно логов
# ---------------------------------------------------------------------------

log_window = None
log_text = None


def open_log_window():
    global log_window, log_text

    import tkinter as tk
    from tkinter import scrolledtext

    if log_window is not None:
        try:
            log_window.deiconify()
            log_window.lift()
            log_window.focus_force()
            return
        except Exception:
            pass

    log_window = tk.Tk()
    log_window.title("TG Project — логи")
    log_window.geometry("1000x600")
    log_window.configure(bg="#0f172a")

    top = tk.Frame(log_window, bg="#0f172a")
    top.pack(fill="x", padx=8, pady=(8, 4))

    tk.Label(
        top,
        text="TG Project — логи контроллера",
        bg="#0f172a",
        fg="#e2e8f0",
        font=("Segoe UI", 11, "bold"),
    ).pack(side="left")

    btn_log = tk.Button(
        top, text="Очистить", command=lambda: log_text.delete("1.0", "end"),
        bg="#1e293b", fg="#e2e8f0", bd=0, relief="flat", cursor="hand2",
    )
    btn_log.pack(side="right")

    log_text = scrolledtext.ScrolledText(
        log_window,
        wrap="word",
        bg="#0b1220",
        fg="#cbd5e1",
        insertbackground="#cbd5e1",
        font=("Consolas", 9),
        relief="flat",
    )
    log_text.pack(fill="both", expand=True, padx=8, pady=(0, 8))

    log_window.protocol("WM_DELETE_WINDOW", hide_log_window)


def hide_log_window():
    global log_window
    if log_window is not None:
        try:
            log_window.withdraw()
        except Exception:
            pass


def pump_logs(controller: ControllerProcess, icon_ref=None) -> None:
    """Переливает вывод контроллера в окно логов (если оно открыто)."""
    while True:
        try:
            line = controller.lines.get(timeout=0.5)
        except queue.Empty:
            continue
        except Exception:
            break

        if line == "__CONTROLLER_EXITED__":
            if log_text is not None:
                try:
                    log_text.insert("end", "\n=== Контроллер остановлен ===\n")
                except Exception:
                    pass
            if icon_ref is not None:
                try:
                    icon_ref.title = "TG Project - остановлен"
                except Exception:
                    pass
            continue

        if log_text is not None:
            try:
                log_text.insert("end", line + "\n")
                log_text.see("end")
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Статус (через API контроллера)
# ---------------------------------------------------------------------------


def fetch_status():
    try:
        import httpx

        r = httpx.get(f"{CONTROLLER_URL}/api/status", timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        return {"error": str(exc)}


def format_status(data: dict) -> str:
    if "error" in data:
        return f"Контроллер недоступен: {data['error']}"

    lines = [f"Режим: {data.get('mode_title', data.get('mode'))}", ""]
    for name, info in (data.get("components") or {}).items():
        icon = "[+] работает" if info.get("running") else "[-] остановлен"
        port = f" :{info['port']}" if info.get("port") else ""
        lines.append(f"{icon}  {name}{port} — {info.get('title', '')}")
    return "\n".join(lines)


def show_status_window() -> None:
    import tkinter as tk

    data = fetch_status()
    text = format_status(data)

    win = tk.Toplevel()
    win.title("TG Project — статус")
    win.geometry("560x320")
    win.configure(bg="#0f172a")
    tk.Label(
        win, text="Состояние проекта", bg="#0f172a", fg="#e2e8f0",
        font=("Segoe UI", 11, "bold"),
    ).pack(anchor="w", padx=12, pady=(12, 4))
    tk.Label(
        win, text=text, bg="#0f172a", fg="#cbd5e1", justify="left",
        font=("Consolas", 10), anchor="w",
    ).pack(fill="both", expand=True, padx=12, pady=(0, 12))


def set_mode(mode: str) -> None:
    try:
        import httpx

        r = httpx.post(f"{CONTROLLER_URL}/api/mode", json={"mode": mode}, timeout=180)
        r.raise_for_status()
        log(f"Режим переключён: {mode}")
    except Exception as exc:
        log(f"Не удалось переключить режим: {exc}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def graceful_exit(icon_obj, controller: ControllerProcess) -> None:
    """Гасит контроллер с ботами и снимает иконку, затем завершает процесс.

    Вынесено на уровень модуля, чтобы поведение можно было проверить тестом.
    """
    log("Выход из трея: останавливаю ботов...")
    try:
        icon_obj.title = "TG Project - останавливаю..."
    except Exception:
        pass
    # Иконку убираем сразу - она не должна висеть после нажатия "Выход"
    try:
        icon_obj.visible = False
    except Exception:
        pass

    try:
        controller.stop()
    except Exception as exc:
        log(f"Ошибка остановки: {exc}")

    try:
        icon_obj.stop()
    except Exception:
        pass

    time.sleep(0.5)
    log("Трей-лаунчер остановлен")
    os._exit(0)


def main() -> int:
    import pystray
    from pystray import Menu, MenuItem

    controller = ControllerProcess()

    if not controller.start():
        log("✗ Запуск не удался")
        return 1

    def do_exit(icon_obj, item=None):
        threading.Thread(
            target=graceful_exit, args=(icon_obj, controller), daemon=True
        ).start()

    icon = pystray.Icon(
        "tg_bot",
        make_icon_image(),
        "TG Project — боты запущены",
        menu=Menu(
            MenuItem("Статус", lambda: show_status_window(), default=True),
            MenuItem("Показать логи", lambda: open_log_window()),
            MenuItem("Перезапустить", lambda: (log("Перезапускаю…"), controller.restart())),
            Menu.SEPARATOR,
            MenuItem(
                "Режим",
                Menu(
                    MenuItem("Все (FunPay + PlayerOK)", lambda: set_mode("all")),
                    MenuItem("FunPay", lambda: set_mode("funpay")),
                    MenuItem("PlayerOK", lambda: set_mode("playerok")),
                    MenuItem("FunPay Watcher", lambda: set_mode("watcher")),
                    MenuItem("Остановить бота", lambda: set_mode("off")),
                ),
            ),
            Menu.SEPARATOR,
            MenuItem("Выход", do_exit),
        ),
    )

    icon.on_exit = do_exit

    # поток логов стартует после создания иконки, чтобы обновлять её подсказку
    threading.Thread(target=pump_logs, args=(controller, icon), daemon=True).start()

    icon.run()

    log("Трей-лаунчер остановлен")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback

        tb = traceback.format_exc()
        log(f"✗ Критическая ошибка:\n{tb}")
        try:
            with open(os.path.join(BASE_DIR, "tray_error.log"), "w", encoding="utf-8") as fh:
                fh.write(tb)
        except Exception:
            pass
        sys.exit(1)
