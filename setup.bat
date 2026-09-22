@echo off
REM ============================================================
REM  One-time local setup. Run this after copying the project to
REM  a new machine (or on a fresh clone) -- it creates the project
REM  virtualenv and installs both Python and Node dependencies.
REM
REM      setup.bat
REM
REM  Idempotent: safe to run again any time.
REM  ASCII-only on purpose (the project path may be non-ASCII).
REM ============================================================
setlocal
chcp 65001 >nul

call "%~dp0_env.bat"

echo ============================================================
echo   Project setup
echo ============================================================
echo   ROOT : %ROOT%
echo.

REM ---------- 1. base interpreter ----------
REM  Prefer the project venv if it already exists (re-running setup
REM  should top up the existing env, not rebuild it).
set "BASE_PY="
if exist "%VENV%\Scripts\python.exe" set "BASE_PY=%VENV%\Scripts\python.exe"
if not defined BASE_PY if defined PYTHON if exist "%PYTHON%" set "BASE_PY=%PYTHON%"
if not defined BASE_PY for %%I in (python.exe) do set "BASE_PY=%%~$PATH:I"

if not defined BASE_PY (
  echo [ERROR] No Python interpreter found.
  echo.
  echo   Install Python 3.11 - 3.13 and tick "Add python.exe to PATH",
  echo   or point the PYTHON environment variable at an existing install:
  echo.
  echo       set PYTHON=C:\path\to\python.exe
  echo.
  pause
  exit /b 1
)

echo [1/3] Python : %BASE_PY%
"%BASE_PY%" -V
echo.

REM ---------- 2. virtualenv + python deps ----------
if not exist "%VENV%\Scripts\python.exe" (
  echo       Creating virtualenv at backend\.venv ...
  "%BASE_PY%" -m venv "%VENV%"
  if errorlevel 1 (
    echo [ERROR] Failed to create the virtualenv.
    pause
    exit /b 1
  )
) else (
  echo       Virtualenv already exists - reusing it.
)

set "VPY=%VENV%\Scripts\python.exe"

echo       Installing Python dependencies ^(this can take a few minutes^) ...
"%VPY%" -m pip install --upgrade pip --no-cache-dir --quiet
"%VPY%" -m pip install -r "%ROOT%\backend\requirements.txt" --no-cache-dir
if errorlevel 1 (
  echo [ERROR] pip install failed. Check your network / proxy and retry.
  pause
  exit /b 1
)
echo       Python dependencies OK.
echo.

REM ---------- 3. frontend deps ----------
echo [2/3] Node   : 
if not defined NPM (
  echo       [SKIP] npm not found on PATH. Install Node.js ^>= 20, then run:
  echo              cd /d "%ROOT%\frontend" ^&^& npm install
) else (
  "%NPM%" -v
  if exist "%ROOT%\frontend\node_modules" (
    echo       frontend\node_modules already exists - skipping npm install.
    echo       ^(delete that folder and re-run setup.bat to force a reinstall^)
  ) else (
    pushd "%ROOT%\frontend"
    "%NPM%" install --no-audit --no-fund
    if errorlevel 1 (
      echo [ERROR] npm install failed.
      popd
      pause
      exit /b 1
    )
    popd
    echo       Frontend dependencies OK.
  )
)
echo.

REM ---------- verify ----------
echo [3/3] Verifying backend imports ...
"%VPY%" -c "import fastapi,uvicorn,duckdb,baostock,akshare,pandas,pyarrow; print('  all backend dependencies import OK')"
if errorlevel 1 (
  echo [ERROR] Dependency verification failed.
  pause
  exit /b 1
)

echo.
echo ============================================================
echo   Setup complete.
echo   Next: double-click start-dev.bat  (or start-service.bat)
echo ============================================================
pause
endlocal
