@echo off
cd /d "%~dp0"
if not exist .venv python -m venv .venv
call .venv\Scripts\activate
python -m pip install -r requirements.txt
if not exist .env copy .env.example .env
echo.
echo Set APP_PASSWORD and HH_USER_AGENT in the .env file before exposing the application publicly.
echo Open http://127.0.0.1:8000 after the server starts.
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1
pause
