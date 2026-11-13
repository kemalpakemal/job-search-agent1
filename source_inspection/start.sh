#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -d .venv ]; then python3 -m venv .venv; fi
source .venv/bin/activate
python -m pip install -r requirements.txt
if [ ! -f .env ]; then cp .env.example .env; fi
printf '\nОткройте .env, задайте APP_PASSWORD и HH_USER_AGENT, затем запустите скрипт повторно.\n'
read -r -p 'Нажмите Enter, чтобы запустить приложение... ' _ || true
exec uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1
