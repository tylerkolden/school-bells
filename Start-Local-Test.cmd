@echo off
setlocal
cd /d "%~dp0"

docker info >nul 2>&1
if errorlevel 1 (
  echo Docker Desktop is not running. Start Docker Desktop, wait for it to say the engine is ready,
  echo and then double-click this file again.
  pause
  exit /b 1
)

echo Building and starting the safe local School Bell test environment...
docker compose up --build --detach --wait --wait-timeout 300
if errorlevel 1 (
  echo.
  echo Startup failed. Run: docker compose logs
  echo If an older saved test destination is unhealthy, run Reset-Local-Test.cmd.
  pause
  exit /b 1
)

echo.
echo School Bell UI: http://localhost:8080
echo Local receiver: http://localhost:9000
echo Default password: local-test-only
start "" http://localhost:8080
pause
