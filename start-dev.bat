@echo off
REM ============================================================
REM  Dev mode launcher: backend (uvicorn --reload) + frontend
REM  (vite dev), each in its own window.
REM
REM      start-dev.bat
REM
REM  ASCII-only on purpose: the project path may contain
REM  non-ASCII characters, so nothing here hardcodes a path.
REM  The interpreter is discovered by _env.bat (project venv
REM  first, then %PYTHON%, then PATH).
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
  echo.
  echo         Run setup.bat once to create backend\.venv and install
  echo         requirements.txt ^(about 2-5 minutes, needs network^).
  echo.
  pause
  exit /b 1
)

if not exist "%ROOT%\frontend\node_modules" (
  echo [ERROR] frontend\node_modules is missing.
  echo         Run setup.bat once, or:  cd /d "%ROOT%\frontend" ^&^& npm install
  echo.
  pause
  exit /b 1
)

echo ============================================================
echo   Dev mode
echo ============================================================
echo   python   : %PY%
echo   backend  : http://127.0.0.1:8000   ^(API docs at /docs^)
echo   frontend : http://127.0.0.1:5173
echo ============================================================
echo.

REM ---------- backend ----------
REM pushd so the spawned window inherits backend\ as its working directory
REM (`python -m uvicorn app.main:app` resolves the `app` package from the cwd).
REM Deliberately no `start /D ...` -- this keeps the start line in the plain
REM `start "title" cmd /k "command"` form, which is the least fragile.
pushd "%ROOT%\backend"
start "stock-backend" cmd /k ""%PY%" -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000"
popd

timeout /t 4 /nobreak >nul

REM ---------- frontend ----------
pushd "%ROOT%\frontend"
start "stock-frontend" cmd /k ""%NPM%" run dev"
popd

echo Both services launched in separate windows.
echo Close those windows to stop the services.
echo.
echo NOTE: the backend catches up missing trading days on startup.
echo If the "stock-backend" window prints "N missing trading days",
echo it is backfilling (~30-90 min) and the API is not up yet -
echo wait for "startup complete" / "Application startup complete".
echo.
echo If the backend window closes immediately, run setup.bat and retry.
endlocal
