@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo  saezuri のセットアップ（初回だけ実行します）
echo ============================================
echo.
rem 対応するPythonは 3.11〜3.14。3.15以上は必要な部品（PyYAML）がまだ対応していないため使わない。
set "PY_RANGE=import sys; sys.exit(0 if (3, 11) <= sys.version_info[:2] <= (3, 14) else 1)"
set "PYVER_LIST=3.14 3.13 3.12 3.11"

where py >nul 2>nul
if errorlevel 1 goto no_python

rem 前回のセットアップで対応外のPythonで作った環境が残っていたら作り直す
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "%PY_RANGE%" >nul 2>nul
  if errorlevel 1 (
    echo 前回のセットアップで作った作業用の環境が使えないため、作り直します...
    rmdir /s /q ".venv"
  )
)
if exist ".venv\Scripts\python.exe" goto install

call :find_python
if not defined PYVER (
  rem Python install manager（新しいpython.orgのインストーラー）なら、対応する版をその場で追加できる
  py help install >nul 2>nul
  if not errorlevel 1 (
    echo 対応する版の Python（3.11〜3.14）が入っていないため、Python 3.14 を追加します...
    py install 3.14
    call :find_python
  )
)
if not defined PYVER goto wrong_python

echo [1/3] 作業用の環境を作っています（Python %PYVER%）...
py -%PYVER% -m venv .venv
if errorlevel 1 goto fail

:install
if not exist "config.yaml" (
  if exist "config.example.yaml" copy /y "config.example.yaml" "config.yaml" >nul
)
echo [2/3] 必要な部品をインストールしています（数分かかります）...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 goto fail
echo [3/3] 収集用のブラウザをインストールしています（数分かかります）...
".venv\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 goto fail
echo.
echo セットアップが終わりました。start_gui.bat をダブルクリックすると画面が開きます。
echo.
pause
exit /b 0

:find_python
set "PYVER="
for %%v in (%PYVER_LIST%) do (
  if not defined PYVER (
    py -%%v -c "%PY_RANGE%" >nul 2>nul && set "PYVER=%%v"
  )
)
exit /b 0

:no_python
echo Python が見つかりません。
echo 次のページから Python 3.14 をインストールしてから、もう一度この setup.bat をダブルクリックしてください。
echo   https://www.python.org/downloads/windows/
echo   （「Python 3.14.x」の「Download Windows installer (64-bit)」を選びます）
echo （インストール画面の「Add python.exe to PATH」にチェックを入れてください）
echo.
pause
exit /b 1

:wrong_python
echo 対応する版の Python が見つかりません（使えるのは 3.11〜3.14 です）。
echo 3.15 以上は、必要な部品がまだ対応していないため使えません。
echo 次のページから Python 3.14 をインストールしてから、もう一度この setup.bat をダブルクリックしてください。
echo   https://www.python.org/downloads/windows/
echo   （「Python 3.14.x」の「Download Windows installer (64-bit)」を選びます）
echo すでに入っている Python はそのままで構いません（並べて使えます）。
echo.
pause
exit /b 1

:fail
echo.
echo セットアップの途中で失敗しました。上に出ているメッセージを確認してください。
echo （ネットワークにつながっているか、会社のPCでインストールが制限されていないか等）
echo.
pause
exit /b 1
