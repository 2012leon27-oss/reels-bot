#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export APP_ENV=development
export PATH="$HOME/.local/bin:$PATH"
if [[ ! -f secrets/api.key ]] || [[ ! -s secrets/api.key ]]; then
  echo "Положи CURSOR_API_KEY в secrets/api.key"
  echo "Ключ берётся здесь: https://cursor.com/dashboard/api"
  exit 1
fi
python3 -m pip install -q -r requirements.txt
python3 bot.py
