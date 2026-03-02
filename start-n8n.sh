#!/bin/bash
# Запуск n8n локально (без Docker)
# Данные workflow сохраняются в ~/.n8n

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ -f "$SCRIPT_DIR/.env" ]; then
  echo "Loading .env..."
  export $(grep -v '^#' "$SCRIPT_DIR/.env" | xargs)
fi

echo "Starting n8n on http://localhost:5678"
echo "Press Ctrl+C to stop"
echo ""

npx n8n start
