@echo off
setlocal
cd /d "%~dp0"
docker compose down
if errorlevel 1 (
  echo Docker could not stop the local test environment.
  pause
  exit /b 1
)
echo The local test environment is stopped. Your test configuration and history were preserved.
pause
