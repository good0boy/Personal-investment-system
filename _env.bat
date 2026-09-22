@echo off
REM ============================================================
REM  Shared environment probe (sourced, not run directly).
REM
REM      call "%~dp0_env.bat"
REM
REM  Exports to the caller:
REM      ROOT   project root  (this file's own directory)
REM      VENV   project virtualenv (backend\.venv)
REM      PY     python interpreter
REM      NPM    npm command
REM      PY_OK  1 = interpreter has all project dependencies
REM
REM  ASCII-only on purpose: the project path may contain
REM  non-ASCII characters, so nothing in here may hardcode a
REM  path. Everything is derived from %~dp0 or from PATH.
REM
REM  DO NOT add "setlocal" -- variable assignments must leak
REM  back to the calling script.
REM ============================================================

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

set "VENV=%ROOT%\backend\.venv"

REM ---------- Python: project venv -> %PYTHON% -> PATH ----------
set "PY="
if exist "%VENV%\Scripts\python.exe" set "PY=%VENV%\Scripts\python.exe"
if not defined PY if defined PYTHON if exist "%PYTHON%" set "PY=%PYTHON%"
if not defined PY for %%I in (python.exe) do set "PY=%%~$PATH:I"

REM ---------- Node / npm ----------
set "NPM="
for %%I in (npm.cmd) do set "NPM=%%~$PATH:I"
if not defined NPM if defined NODE_HOME if exist "%NODE_HOME%\npm.cmd" set "NPM=%NODE_HOME%\npm.cmd"

REM ---------- Does the interpreter actually have the deps? ----------
REM  Probing here turns "service dies with ModuleNotFoundError three
REM  seconds later" into an actionable message before anything starts.
set "PY_OK=0"
if not defined PY goto :eof
"%PY%" -c "import fastapi,uvicorn,duckdb,baostock,akshare,pandas,pyarrow" >nul 2>nul
if not errorlevel 1 set "PY_OK=1"
goto :eof
