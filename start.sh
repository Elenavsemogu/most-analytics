#!/bin/bash
# Запуск MOST Analytics Dashboard
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -f ".env" ]; then
    echo "Создаю .env из шаблона..."
    cp .env.example .env
    echo "ЗАПОЛНИ .env своими ключами, потом запусти снова."
    exit 1
fi

if ! python3 -c "import fastapi" 2>/dev/null; then
    echo "Устанавливаю зависимости..."
    pip3 install -r requirements.txt
fi

echo ""
echo "  MOST Analytics Dashboard"
echo "  http://localhost:8090"
echo ""

python3 -m uvicorn server:app --reload --port 8090
