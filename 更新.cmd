@echo off
rem ダム旅 — 貯水率を更新して damtabi.com に反映する
rem このファイルをダブルクリックするだけで済むようにしてあります。
chcp 65001 > nul
title ダム旅 - 貯水率の更新
cd /d "%~dp0"

set PY=
where py >nul 2>&1 && set PY=py -3
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
