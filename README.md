# PlayerOK NFT Trading Bot

Telegram-бот, который мониторит **PlayerOK** (маркетплейс Telegram NFT), распознаёт
подарок на картинке через ИИ, сравнивает цену с рыночной и присылает в чат только
**выгодные** лоты.

```
Python 3.10+ · aiogram 3 · FastAPI · работает на Windows / Linux / Termux
```

---

## Что умеет

* **Мониторинг** PlayerOK: каждые N секунд обходит лоты, следит за ценой.
* **Распознавание подарка** по фото: коллекция, модель, фон, номер (`#12345`).
* **Оценка**: берёт реальные продажи (историю) и минимальные цены в продаже,
  считает профит с учётом комиссии сети 3.5 USD.
* **Фильтр мусора** — в чат попадает только то, что реально стоит брать:
  * лоты без рыночных данных — отбрасываются;
  * лоты, где цена посчитана лишь по минимальной цене в продаже (а не по реальным
    продажам) — отбрасываются, профит по ним не гарантирован;
  * лоты без профита (или с профитом меньше заданного порога) — отбрасываются.
* **Управление из Telegram**: `/mode`, `/settings`, `/ai`, `/bots`.
* **Иконка в трее** (Windows) — запуск, логи, статус, остановка.

---

## Быстрый старт

### 1. Настройка `.env`

```bash
cp .env.example .env
```

Заполни обязательные значения:

| Переменная | Что это | Обязательно |
|---|---|---|
| `BOT_TOKEN` | токен бота от [@BotFather](https://t.me/BotFather) | да |
| `PLAYEROK_COOKIE` | куки авторизованной сессии playerok.com | да |
| `NOTIFICATION_GROUP_ID` | id чата, куда слать алерты (например `-1001234567890`) | да |
| `CONTROL_ALLOWED_USERS` | твои Telegram id через запятую — доступ к `/mode`, `/settings`, `/ai` | да |
| `GIFT_SATELLITE_TOKENS` | токены API gift-satellite.dev через запятую | для цен |
| `GEMINI_KEYS` | ключи Google AI Studio через запятую | для распознавания |
| `OPENROUTER_API_KEY` | ключ [openrouter.ai](https://openrouter.ai/keys) — запас на случай, когда кончится квота Gemini | нет |
| `AI_PROVIDERS` | порядок провайдеров ИИ | нет |

**Как взять `PLAYEROK_COOKIE`:** залогинься на playerok.com → DevTools →
Application → Cookies → скопируй строку `cookie` целиком.

### 2. Запуск

```bash
pip install -r requirements.txt
```

**Windows:** двойной клик по `start.bat` — появится иконка в трее.
**Linux/macOS (из корня проекта):**

```bash
python -m playerok_bot
```

---

## Команды бота

| Команда | Что делает |
|---|---|
| `/mode` | текущий режим + кнопки переключения |
| `/mode all\|playerok\|off` | переключить режим |
| `/bots` | что запущено, порты, PID |
| `/settings` | настройки отсева + кнопки |
| `/profitable on\|off` | только выгодные предложения |
| `/margin 10` | минимальная маржа выгоды, % |
| `/minprofit 500` | минимальная прибыль, ₽ |
| `/ai` | статус провайдеров ИИ, приоритет, тест |

---

## Настройки отсева (`settings.json`)

```json
{
  "only_profitable": false,
  "profit_margin_pct": 5.0,
  "min_profit_rub": 0.0,
  "skip_no_price_data": true
}
```

* `only_profitable` — показывать только лоты с гарантированным профитом;
* `profit_margin_pct` — минимальная маржа в процентах от рыночной цены;
* `min_profit_rub` — минимальная прибыль в рублях;
* `skip_no_price_data` — не присылать лоты, по которым нет рыночных данных.

**Отложенная отправка:** пока включён любой из фильтров («Только выгодные»
или «Пропускать без цен»), бот вообще не отправляет лот, пока не посчитает
цены и профит. В чат уходит только прошедший проверки лот — предложений,
которые пришлось бы удалять через несколько секунд, больше нет.
Когда оба фильтра выключены, работа идёт в «живом» режиме: сообщение
появляется сразу и обновляется по мере сбора данных.

---

## Провайдеры ИИ

Бот умеет работать с несколькими облачными провайдерами и **сам переключается**,
когда у текущего кончилась квота:

| Провайдер | Картинки | Ключ |
|---|---|---|
| Gemini (через локальный прокси) | да | бесплатная квота Google AI Studio |
| OpenRouter | да | [openrouter.ai/keys](https://openrouter.ai/keys) |
| NVIDIA NIM | да | [build.nvidia.com](https://build.nvidia.com) |
| Cloudflare Workers AI | да | [dash.cloudflare.com](https://dash.cloudflare.com) |
| Pollinations | да | [enter.pollinations.ai/key](https://enter.pollinations.ai/key) |
| Groq | нет (только текст) | [console.groq.com](https://console.groq.com) |

Порядок задаётся переменной `AI_PROVIDERS` или командой `/ai`.

---

## Архитектура

```
playerok-bot/
├── start.bat, requirements.txt, .env.example
└── playerok_bot/                  # основной пакет
    ├── controller.py      :9324  — супервизор: сервисы, боты, статус
    ├── tray_launcher.py           — иконка в трее (Windows), логи, остановка
    ├── botPl.py                   — сам бот PlayerOK (aiogram, long polling)
    ├── ai/
    │   ├── ai_providers.py        — общий слой над провайдерами ИИ
    │   ├── airequests.py          — запросы к ИИ
    │   └── _dotenv.py             — загрузка .env
    ├── servers/
    │   ├── market_server.py  :9321 — прокси gift-satellite.dev (цены, история)
    │   └── gemini_server.py  :9322 — прокси Gemini (ротация ключей, лимиты)
    ├── control/
    │   ├── bot_control.py         — команды /mode, /bots, /settings, /ai
    │   └── app_settings.py        — общие настройки (settings.json)
    ├── utils.py                   — хелперы и безопасная работа с JSON
    └── paths.py                   — пути: всё состояние в корне проекта
```

Файлы состояния (`.env`, `settings.json`, `ai_order.json`, `gemini_stats.json`,
`gemini_workspace/`, логи) живут в корне проекта и в git не попадают —
смотри `.gitignore`.

---

## Частые вопросы

**Бот не пишет в чат.** Проверь, что бот — админ чата `NOTIFICATION_GROUP_ID`
и есть право отправлять сообщения. В логах будет `chat not found`.

**`429` от Gemini.** Квота Google на сутки выбрана. Проверь `/ai` — если Gemini
красный, поставь бесплатный ключ OpenRouter, и бот продолжит работу на нём.

**Цены не находятся.** Нужен `GIFT_SATELLITE_TOKENS` (токены gift-satellite.dev).

**Ошибка `Bot token is not set` и т.п.** Не заполнен `.env` — скопируй
`.env.example` в `.env` и заполни.

---

## Лицензия

Свободное использование. Автор не несёт ответственности за любые убытки,
связанные с покупкой и перепродажей подарков.
