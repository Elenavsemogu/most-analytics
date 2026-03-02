# Гайд по настройке аналитики MOST

> Пошаговая инструкция. Выполнять сверху вниз.

## Шаг 1: TGStat API-токен

1. Открой [tgstat.ru](https://tgstat.ru) → войди через Telegram
2. Добавь канал MOST: tgstat.ru → «Мои каналы» → «Добавить канал»
3. Перейди: [tgstat.ru/my/profile](https://tgstat.ru/my/profile) → раздел «API»
4. Скопируй токен → вставь в `.env` файл (см. ниже)
5. Проверь (подставь свой токен и username канала):
   ```bash
   curl "https://api.tgstat.ru/channels/get?token=ТВОЙ_ТОКЕН&channelId=@username_канала"
   ```
   Должен вернуть JSON с данными канала.

**Бесплатный тариф** даёт: `channels/get`, `channels/stat`, `channels/posts`, `posts/stat` — этого достаточно для MVP.

## Шаг 2: OpenAI API-ключ

1. Открой [platform.openai.com/api-keys](https://platform.openai.com/api-keys)
2. Create new secret key → скопируй
3. Вставь в `.env`

## Шаг 3: Telegram Bot для отчётов

1. Открой [@BotFather](https://t.me/BotFather) в Telegram
2. `/newbot` → назови, например, `MOST Analytics Bot`
3. Скопируй токен → вставь в `.env`
4. Создай группу/чат для отчётов, добавь туда бота
5. Чтобы получить chat_id, отправь любое сообщение в чат и выполни:
   ```bash
   curl "https://api.telegram.org/botТВОЙ_BOT_TOKEN/getUpdates"
   ```
   В ответе найди `"chat": {"id": -100XXXXXXXXXX}` — это твой chat_id.

## Шаг 4: Запуск n8n

```bash
cd gambling/most-analytics
# Скопируй шаблон env
cp .env.example .env
# Заполни .env своими ключами, потом:
./start-n8n.sh
```

n8n откроется на http://localhost:5678

## Шаг 5: Импорт workflow

1. В n8n: меню → Import from File
2. Выбери `workflow-weekly-report.json`
3. Создай Credentials:
   - **Header Auth** для TGStat (не нужно, токен в query)
   - **OpenAI API** → вставь ключ
   - **Telegram API** → вставь bot token
4. В узле «Set Variables» впиши:
   - `channel_id` — username канала MOST
   - `tgstat_token` — токен TGStat
5. В узле «Telegram: Send» укажи chat_id

## Шаг 6: Тестовый запуск

1. Нажми «Test Workflow» в n8n (Manual Trigger)
2. Проверь каждый узел — клик по узлу → вкладка Output
3. Если всё ОК — отчёт придёт в Telegram

## Шаг 7: Включить автоматизацию

1. В n8n включи узел «Schedule Trigger» (понедельник 09:00)
2. Activate workflow (тумблер сверху)
3. n8n должен быть запущен для работы cron
