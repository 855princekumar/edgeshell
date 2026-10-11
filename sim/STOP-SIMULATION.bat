@echo off
title EdgeShell Simulation - Stopping
cd /d "%~dp0"
echo Stopping EdgeShell simulation...
docker compose -f docker-compose.sim.yml down
echo Done. All containers stopped.
pause
