@echo off
REM ============================================================
REM  Service mode launcher: backend only, no --reload.
REM
REM      start-service.bat
REM
REM  Use this when you want the scheduler (23:00 paper run, 08:00
REM  daily summary, 09:00 catalog check) and the catch-up-on-startup
REM  scan to actually work -- both are unreliable under --reload.
REM
REM  The backend also serves the built frontend, so if
REM  frontend\dist exists this single process gives you the whole
REM  UI on one port (http://127.0.0.1:8000). That is also exactly
REM  how it will run behind nginx on the server.
REM
REM  ASCII-only on purpose (the project path may be non-ASCII).
REM ============================================================
setlocal
chcp 65001 >nul

call "%~dp0_env.bat"

REM ---------- preflight ----------
if not defined PY (
  echo [ERROR] No Python interpreter found.
  echo         Run setup.bat once, or install Python 3.11-3.13 and
  echo         add it to PATH ^(or set the PYTHON environment variable^).
  echo.
  pause
  exit /b 1
)

if "%PY_OK%"=="0" (
  echo [ERROR] Python found at:
  echo             %PY%
  echo         but it is missing this project's dependencies.
  echo         Run setup.bat once to create backend\.venv.
  echo.
  pause
  exit /b 1
)

if exist "%ROOT%\frontend\dist\index.html" (
  set "UI=%ROOT%\frontend\dist"
) else (
  set "UI="
)

echo ============================================================
echo   Service mode  (scheduler enabled, no reload)
echo ============================================================
echo   python  : %PY%
echo   API     : http://127.0.0.1:8000
echo   Docs    : http://127.0.0.1:8000/docs
if defined UI (
  echo   UI      : http://127.0.0.1:8000   ^(serving frontend\dist^)
) else (
  echo   UI      : not built - run "npm run build" in frontend\ first,
  echo             or use start-dev.bat for the vite dev server.
)
echo.
echo   Press Ctrl+C to stop.
echo ============================================================
echo.
echo   NOTE: on the first start the scheduler catches up any missing
echo   trading days (paper run + full-market data backfill, ~30-90 min).
echo   During that time the port is NOT open yet, so the browser will
echo   show "connection refused". The window is working, not frozen:
echo   watch it for "missing trading days" / "startup complete" and wait.
echo   Already up to date? Then startup takes only a few seconds.
echo ============================================================
echo.

pushd "%ROOT%\backend"

REM Serve frontend\dist straight from the backend -> one process, one port.
REM (Ignored when dist is missing; the backend just logs a warning.)
set "SERVE_FRONTEND=1"

"%PY%" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
popd

endlocal
