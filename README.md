# TGMediaDownloader

inline-бот Telegram для скачивания медиа из:
- Instagram
- TikTok
- YouTube

## Быстрый старт

1. Создайте и активируйте venv:
```bash
python -m venv venv
venv\Scripts\activate
```

2. Установите зависимости:
```bash
pip install -r requirements.txt
```

3. Скопируйте `.env.example` в `.env` и заполните:
- `BOT_TOKEN`
- `CACHE_CHAT_ID`
- `MAX_FILE_SIZE_MB` (опционально, по умолчанию `49`)
- `COOKIES_FILE` (опционально)

4. Запустите:
```bash
python bot.py
```

## BotFather

Включите:
- `/setinline`
- `/setinlinefeedback` -> `Enabled`

## Использование

1. В любом чате введите: `@your_bot https://...`
2. Выберите результат бота
3. Бот заменит inline-сообщение на медиа
