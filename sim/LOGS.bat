@echo off
title EdgeShell Simulation Logs
cd /d "%~dp0"
echo Streaming logs (Ctrl+C to stop)...
docker compose -f docker-compose.sim.yml logs -f
