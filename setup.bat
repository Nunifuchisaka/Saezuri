@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo  saezuri のセットアップ（初回だけ実行します）
echo ============================================
echo.
where py >nul 2>nul
if errorlevel 1 (
  echo Python が見つかりません。
  echo https://www.python.org/downloads/ から Python 3.11 以上をインストールしてから、
  echo もう一度この setup.bat をダブルクリックしてください。
  echo （インストール画面の「Add python.exe to PATH」にチェックを入れてください）
  echo.
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  echo [1/3] 作業用の環境を作っています...
  py -3 -m venv .venv
  if errorlevel 1 goto fail
)
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

:fail
echo.
echo セットアップの途中で失敗しました。上に出ているメッセージを確認してください。
echo （ネットワークにつながっているか、会社のPCでインストールが制限されていないか等）
echo.
pause
exit /b 1
