@echo off
REM ===================================================================
REM  Reconize - NETWORK mode
REM
REM  Serves the whole app (pages AND api) from ONE port, bound to every
REM  network interface, so other computers on the same cable or Wi-Fi
REM  network can use it.
REM
REM  Only THIS machine runs Reconize. The others just open a browser.
REM  That means one database and one photo store, shared by everyone -
REM  there is nothing to sync and nothing to keep in step.
REM
REM  Use start.bat instead for normal local work: it runs the Vite dev
REM  server with hot reload and stays on localhost only.
REM ===================================================================
cd /d "%~dp0"

echo Building the frontend so the served app is up to date...
pushd frontend
call npm run build
if errorlevel 1 (
    echo.
    echo Build FAILED - not starting. Fix the error above and run this again.
    popd
    pause
    exit /b 1
)
popd

echo.
echo ===================================================================
echo  Open Reconize on THIS computer:
echo     http://localhost:8000
echo.
echo  Open it on ANOTHER computer on the same network - use whichever
echo  address matches the network you are both on:
echo.
powershell -NoProfile -Command "Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -ne '127.0.0.1' -and $_.PrefixOrigin -ne 'WellKnown' } | ForEach-Object { $a = Get-NetAdapter -InterfaceIndex $_.InterfaceIndex -ErrorAction SilentlyContinue; if ($a -and $a.Status -eq 'Up') { '     http://' + $_.IPAddress + ':8000    (' + $a.Name + ')' } }"
echo.
echo  Station links work from any of those machines - open Activities,
echo  pick a camera per activity, and press Open station on each.
echo.
echo  If another computer cannot connect it is almost always the Windows
echo  Firewall on THIS machine: allow Python on private networks. Also
echo  check both machines really are on the same network.
echo.
echo  SECURITY: on a shared network anyone who can reach this address
echo  can see participant photos and PDPA records. Change the admin
echo  password in .env before using this at a real event.
echo ===================================================================
echo.

cd backend
.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000
pause
