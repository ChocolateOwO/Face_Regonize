@echo off
cd /d "%~dp0"

echo Starting Reconize backend (http://127.0.0.1:8000)...
start "Reconize Backend" cmd /k "cd backend && .venv\Scripts\python.exe -m uvicorn app.main:app --port 8000"

echo Starting Reconize frontend (http://localhost:5173)...
start "Reconize Frontend" cmd /k "cd frontend && npm run dev"

echo Waiting for servers to start...
timeout /t 6 /nobreak >nul

start chrome http://localhost:5173
