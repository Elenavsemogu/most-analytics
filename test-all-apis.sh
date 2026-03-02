#!/bin/bash
# Полный тест всех API: TGStat + OpenAI + Telegram Bot
# Проверяет каждый сервис отдельно, чтобы при отладке n8n workflow
# было понятно какой именно компонент не работает.
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ -f "$SCRIPT_DIR/.env" ]; then
  export $(grep -v '^#' "$SCRIPT_DIR/.env" | xargs)
fi

PASS=0
FAIL=0

check() {
  if [ $? -eq 0 ]; then
    echo "  ✓ OK"
    PASS=$((PASS + 1))
  else
    echo "  ✗ FAIL"
    FAIL=$((FAIL + 1))
  fi
}

echo "============================================"
echo "  Тест API для MOST Analytics"
echo "============================================"
echo ""

# --- TGStat ---
echo "--- 1/4: TGStat channels/get ---"
if [ -z "$TGSTAT_TOKEN" ] || [ "$TGSTAT_TOKEN" = "your_tgstat_token_here" ]; then
  echo "  ✗ SKIP: TGSTAT_TOKEN не задан в .env"
  FAIL=$((FAIL + 1))
else
  RESP=$(curl -s "https://api.tgstat.ru/channels/get?token=$TGSTAT_TOKEN&channelId=$MOST_CHANNEL_ID")
  echo "$RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f\"  Канал: {d.get('response',{}).get('title','?')}\")" 2>/dev/null
  echo "$RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); assert d.get('status')=='ok' or 'response' in d" 2>/dev/null
  check
fi

echo ""
echo "--- 2/4: TGStat channels/stat ---"
if [ -z "$TGSTAT_TOKEN" ] || [ "$TGSTAT_TOKEN" = "your_tgstat_token_here" ]; then
  echo "  ✗ SKIP: TGSTAT_TOKEN не задан в .env"
  FAIL=$((FAIL + 1))
else
  RESP=$(curl -s "https://api.tgstat.ru/channels/stat?token=$TGSTAT_TOKEN&channelId=$MOST_CHANNEL_ID")
  echo "$RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); r=d.get('response',{}); print(f\"  Подписчики: {r.get('participants_count','?')}, Охват: {r.get('avg_post_reach','?')}\")" 2>/dev/null
  check
fi

echo ""
echo "--- 3/4: OpenAI API ---"
if [ -z "$OPENAI_API_KEY" ] || [ "$OPENAI_API_KEY" = "sk-your_openai_key_here" ]; then
  echo "  ✗ SKIP: OPENAI_API_KEY не задан в .env"
  FAIL=$((FAIL + 1))
else
  RESP=$(curl -s "https://api.openai.com/v1/chat/completions" \
    -H "Authorization: Bearer $OPENAI_API_KEY" \
    -H "Content-Type: application/json" \
    -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"Ответь одним словом: работает?"}],"max_tokens":10}')
  echo "$RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f\"  GPT: {d['choices'][0]['message']['content']}\")" 2>/dev/null
  check
fi

echo ""
echo "--- 4/4: Telegram Bot ---"
if [ -z "$TELEGRAM_BOT_TOKEN" ] || [ "$TELEGRAM_BOT_TOKEN" = "123456789:ABCdefGHIjklMNOpqrsTUVwxyz" ]; then
  echo "  ✗ SKIP: TELEGRAM_BOT_TOKEN не задан в .env"
  FAIL=$((FAIL + 1))
else
  RESP=$(curl -s "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getMe")
  echo "$RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f\"  Бот: @{d['result']['username']}\")" 2>/dev/null
  echo "$RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); assert d.get('ok')==True" 2>/dev/null
  check
fi

echo ""
echo "============================================"
echo "  Результат: $PASS passed, $FAIL failed"
echo "============================================"

if [ $FAIL -gt 0 ]; then
  echo ""
  echo "Заполни недостающие ключи в .env и запусти снова."
  exit 1
fi
