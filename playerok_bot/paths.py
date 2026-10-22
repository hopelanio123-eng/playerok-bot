# -*- coding: utf-8 -*-
"""paths.py — общие пути проекта.

Файлы состояния (.env, settings.json, ai_order.json, gemini_stats.json,
gemini_workspace/, control_state.json, controller.pid, *.log) живут в корне
проекта — рядом со start.bat — и не зависят от того, где внутри пакета
находится модуль, который их читает.
"""

import os

#: Папка самого пакета (playerok_bot/).
PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))

#: Корень проекта (на уровень выше пакета).
PROJECT_DIR = os.path.dirname(PACKAGE_DIR)


def project_file(name: str) -> str:
    """Абсолютный путь к файлу состояния в корне проекта."""
    return os.path.join(PROJECT_DIR, name)
