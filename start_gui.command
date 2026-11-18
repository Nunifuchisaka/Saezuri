#!/bin/bash
# saezuri の画面を開く（macOS用）。Finderでダブルクリックする。
cd "$(dirname "$0")" || exit 1
if [ ! -x ".venv/bin/python" ]; then
  echo "先に setup.command をダブルクリックしてセットアップしてください。"
  read -r -p "Enterキーを押すと閉じます..." _
  exit 1
fi
# 画面はターミナルと切り離して起動する（このターミナルの窓は閉じてよい）。
# 起動に失敗したときの手がかりは logs/gui.log に残す。
mkdir -p logs
nohup .venv/bin/python scripts/gui.py >logs/gui.log 2>&1 &
PID=$!
disown
# 起動直後に落ちていないか、少し待って確かめる
sleep 3
if kill -0 "$PID" 2>/dev/null; then
  echo "saezuri の画面を起動しました。このターミナルの窓は閉じてかまいません。"
else
  echo "画面を起動できませんでした。以下のメッセージを確認してください（logs/gui.log）:"
  echo
  cat logs/gui.log
  echo
  read -r -p "Enterキーを押すと閉じます..." _
  exit 1
fi
