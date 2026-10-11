@echo off
title EdgeShell Fleet Simulation

echo.
echo  =====================================================
echo   EdgeShell Fleet Simulation - Windows Launcher
echo  =====================================================
echo.

REM Check Docker is running
docker info >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] Docker Desktop is not running.
    echo  Please start Docker Desktop and try again.
    pause
    exit /b 1
)
echo  [OK] Docker Desktop is running.

REM Navigate to sim directory
cd /d "%~dp0"

echo  [..] Building and starting simulation...
echo       This takes 2-3 minutes on first run (downloading images)
echo.

docker compose -f docker-compose.sim.yml up --build -d

if errorlevel 1 (
    echo.
    echo  [ERROR] Failed to start simulation.
    echo  Check the error above and ensure ports 5000, 2221-2223, 1881-1883 are free.
    pause
    exit /b 1
)

echo.
echo  [OK] Containers started. Waiting for EdgeShell to be ready...
timeout /t 15 /nobreak >nul

REM Seed nodes automatically
echo  [..] Seeding fleet nodes into EdgeShell...
python ..\run.py 2>nul || python scripts\seed-nodes.py 2>nul
python scripts\seed-nodes.py

echo.
echo  Opening EdgeShell in your browser...
timeout /t 2 /nobreak >nul
start http://localhost:5000

echo.
echo  Simulation is running. Close this window to keep it running.
echo  To stop: run STOP-SIMULATION.bat
echo.
pause
