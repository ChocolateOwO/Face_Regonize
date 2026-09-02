@echo off
REM Restarts ONLY the backend (leaves the frontend dev server alone).
REM Use this after editing .env or backend code.
cd /d "%~dp0"

echo Stopping anything listening on port 8000...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do (
    echo   killing PID %%p
    taskkill /PID %%p /F >nul 2>&1
)

echo Starting Reconize backend (http://127.0.0.1:8000)...
REM Run from backend\ so DATABASE_URL/STORAGE_PATH resolve to backend\database
REM and backend\storage - the same data start.bat uses.
start "Reconize Backend" cmd /k "cd backend && .venv\Scripts\python.exe -m uvicorn app.main:app --port 8000"

echo.
echo Backend is loading the face model - this takes about 15 seconds.
echo Wait for "Application startup complete" in the new window.
echo (Red CUDA / onnxruntime warnings are normal - it falls back to CPU.)
echo.
pause
