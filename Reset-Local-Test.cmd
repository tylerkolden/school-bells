@echo off
setlocal
cd /d "%~dp0"

echo This removes the LOCAL Docker test configuration, sounds, history, branding, and accounts.
echo It does not touch the Raspberry Pi or any production installation.
set /p confirm=Type RESET to continue:
if /i not "%confirm%"=="RESET" (
  echo Reset cancelled.
  pause
  exit /b 1
)

docker compose down --volumes
if errorlevel 1 (
  echo Docker could not remove the local test environment.
  pause
  exit /b 1
)

docker compose up --build --detach --wait --wait-timeout 300
if errorlevel 1 (
  echo Local test startup failed. Run: docker compose logs
  pause
  exit /b 1
)

echo Local test data was reset to the repository defaults.
echo School Bell UI: http://localhost:8080
echo Local receiver: http://localhost:9000
start "" http://localhost:8080
pause
