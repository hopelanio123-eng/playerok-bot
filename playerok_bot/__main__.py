# -*- coding: utf-8 -*-
"""python -m playerok_bot — запускает контроллер проекта.

Эквивалент на Windows:
    python -m playerok_bot.controller
"""

import asyncio

from playerok_bot.controller import main

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nОстановлено пользователем.")
