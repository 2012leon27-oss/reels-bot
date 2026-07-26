#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export APP_ENV=development
if [[ ! -f secrets/api.key ]]; then
  echo "Создай файл secrets/api.key и вставь туда API-ключ."
  exit 1
fi
python3 -m pip install -q -r requirements.txt
python3 bot.py
