# -*- coding: utf-8 -*-
"""PlayerOK NFT trading bot — основной пакет.

Структура:
    playerok_bot.controller      — супервизор: сервисы, боты, статус (:9324)
    playerok_bot.tray_launcher   — иконка в трее (Windows)
    playerok_bot.botPl           — сам трейдинг-бот PlayerOK
    playerok_bot.ai              — ИИ-провайдеры + локальные прокси
    playerok_bot.servers         — market_server (:9321), gemini_server (:9322)
    playerok_bot.control         — команды управления и общие настройки
"""
