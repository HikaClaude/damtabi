@echo off
rem ダム旅 — 貯水率を更新して damtabi.com に反映する
rem このファイルをダブルクリックするだけで済むようにしてあります。
chcp 65001 > nul
title ダム旅 - 貯水率の更新
cd /d "%~dp0"

set PY=
rem (1) per-user Python installed on this PC - does not depend on PATH
if exist "%LOCALAPPDATA%\Programs\Python\Launcher\py.exe" set PY="%LOCALAPPDATA%\Programs\Python\Launcher\py.exe" -3
if not defined PY for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if exist "%%~fD\python.exe" set PY="%%~fD\python.exe"
rem (2) fall back to PATH (previous behaviour)
if not defined PY (where py >nul 2>&1 && set PY=py -3)
if not defined PY (where python >nul 2>&1 && set PY=python)
if not defined PY (
  echo.
  echo   Python が見つかりませんでした。
  echo   https://www.python.org/downloads/ からインストールしてください。
  echo.
  pause
  exit /b 1
)

%PY% scripts\update_and_publish.py %*

echo.
echo   このウィンドウは閉じて構いません。
pause
