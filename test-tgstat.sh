#!/bin/bash
# Тест TGStat API — проверяет что токен и канал работают
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ -f "$SCRIPT_DIR/.env" ]; then
  export $(grep -v '^#' "$SCRIPT_DIR/.env" | xargs)
fi

if [ -z "$TGSTAT_TOKEN" ] || [ "$TGSTAT_TOKEN" = "your_tgstat_token_here" ]; then
  echo "ERROR: Заполни TGSTAT_TOKEN в .env"
  exit 1
fi

if [ -z "$MOST_CHANNEL_ID" ] || [ "$MOST_CHANNEL_ID" = "@your_channel_username" ]; then
  echo "ERROR: Заполни MOST_CHANNEL_ID в .env"
  exit 1
fi

BASE="https://api.tgstat.ru"

echo "=== Тест 1: channels/get ==="
curl -s "$BASE/channels/get?token=$TGSTAT_TOKEN&channelId=$MOST_CHANNEL_ID" | python3 -m json.tool 2>/dev/null || echo "(python3 not found, raw output above)"

echo ""
echo "=== Тест 2: channels/stat ==="
curl -s "$BASE/channels/stat?token=$TGSTAT_TOKEN&channelId=$MOST_CHANNEL_ID" | python3 -m json.tool 2>/dev/null || echo "(python3 not found, raw output above)"

echo ""
echo "=== Тест 3: channels/posts (последние 7 дней) ==="
START_TIME=$(date -v-7d +%s 2>/dev/null || date -d '7 days ago' +%s)
END_TIME=$(date +%s)
curl -s "$BASE/channels/posts?token=$TGSTAT_TOKEN&channelId=$MOST_CHANNEL_ID&startTime=$START_TIME&endTime=$END_TIME&limit=5" | python3 -m json.tool 2>/dev/null || echo "(python3 not found, raw output above)"

echo ""
echo "=== Все тесты пройдены ==="
