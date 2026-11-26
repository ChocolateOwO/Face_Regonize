@echo off
cd /d "%~dp0"

REM Missing setup is reported here, in words, instead of as two windows that
REM close with a Python or npm error. (The Mice hub reads the two "start"
REM lines below to launch Reconize, so keep their exact form.)
if not exist "backend\.venv\Scripts\python.exe" (
    echo Reconize is not set up on this PC yet: backend\.venv is missing.
    echo Run:  powershell -ExecutionPolicy Bypass -File scripts\setup-windows.ps1
    echo       ^(add -Mode cpu on a PC without an NVIDIA GPU^)
    pause
    exit /b 1
)
if not exist "frontend\node_modules" (
    echo The frontend packages are missing: frontend\node_modules.
    echo Run:  powershell -ExecutionPolicy Bypass -File scripts\setup-windows.ps1
    pause
    exit /b 1
)
if not exist ".env" (
    echo WARNING: no .env file - Reconize would start with PUBLIC default passwords.
    echo Run:  backend\.venv\Scripts\python.exe scripts\init_env.py
    pause
    exit /b 1
)

REM The public default JWT_SECRET is reported, not refused: development and
REM test installs may use it on purpose. Real installs must not.
"backend\.venv\Scripts\python.exe" scripts\init_env.py --check >nul 2>&1
if errorlevel 1 (
    echo.
    echo WARNING: .env still uses the PUBLIC default JWT_SECRET - anyone could forge a sign-in.
    echo          Before real use see README.md, "First Sign-in and Passwords".
    echo.
)

echo Starting Reconize backend (http://127.0.0.1:8000)...
start "Reconize Backend" cmd /k "cd backend && .venv\Scripts\python.exe -m uvicorn app.main:app --port 8000"

echo Starting Reconize frontend (http://localhost:5173)...
start "Reconize Frontend" cmd /k "cd frontend && npm run dev"

echo Waiting for servers to start...
timeout /t 6 /nobreak >nul

start "" http://localhost:5173
