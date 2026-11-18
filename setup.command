#!/bin/bash
# saezuri のセットアップ（macOS用。初回だけ実行する）
# Finderでダブルクリックするとターミナルが開いて実行される。
cd "$(dirname "$0")" || exit 1

pause() {
  # ダブルクリックで開いたターミナルがすぐ消えないように待つ（CIなど端末が無いときは待たない）
  if [ -t 0 ] && [ -z "$CI" ]; then
    echo
    read -r -p "Enterキーを押すと閉じます..." _
  fi
}

fail() {
  echo
  echo "セットアップの途中で失敗しました。上に出ているメッセージを確認してください。"
  echo "（ネットワークにつながっているか、会社のMacでインストールが制限されていないか等）"
  pause
  exit 1
}

echo "============================================"
echo " saezuri のセットアップ（初回だけ実行します）"
echo "============================================"
echo

# Python 3.11〜3.14を探す。macOS標準の /usr/bin/python3 は古い（3.9）ことが多いので版を確かめる。
# 3.15以上は、使っている部品（PyYAML）の配布物がまだ無いので避ける。
find_python() {
  local c
  for c in python3.14 python3.13 python3.12 python3.11 python3 \
           /Library/Frameworks/Python.framework/Versions/3.1[1-4]/bin/python3 \
           /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if command -v "$c" >/dev/null 2>&1 &&
       "$c" -c 'import sys; sys.exit(0 if (3, 11) <= sys.version_info[:2] <= (3, 14) else 1)' >/dev/null 2>&1; then
      command -v "$c"
      return 0
    fi
  done
  return 1
}

PY="$(find_python)"
if [ -z "$PY" ]; then
  echo "使える Python（3.11〜3.14）が見つかりません。"
  echo "https://www.python.org/downloads/ から macOS 用の Python 3.14 をインストールしてから、"
  echo "（3.15 以上しか入っていない場合も、3.14 を追加でインストールしてください。両方入れておいて問題ありません）"
  echo "もう一度この setup.command をダブルクリックしてください。"
  pause
  exit 1
fi
echo "使う Python: $PY ($("$PY" -c 'import platform; print(platform.python_version())'))"

# python.org版のPythonは、最初に証明書を入れないとHTTPS通信（投稿の数字・画像の保存）が失敗する。
PY_VER="$("$PY" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
CERT_CMD="/Applications/Python ${PY_VER}/Install Certificates.command"
if ! "$PY" -c 'import ssl, os, sys; p = ssl.get_default_verify_paths(); sys.exit(0 if (p.cafile and os.path.exists(p.cafile)) or (p.capath and os.path.isdir(p.capath) and os.listdir(p.capath)) else 1)' >/dev/null 2>&1; then
  if [ -f "$CERT_CMD" ]; then
    echo "[0/3] HTTPS用の証明書を入れています（Python付属の「Install Certificates.command」で certifi を入れます）..."
    bash "$CERT_CMD" || echo "（証明書の設定に失敗しました。あとで「$CERT_CMD」をダブルクリックしてください）"
  fi
fi

if [ ! -x ".venv/bin/python" ]; then
  echo "[1/3] 作業用の環境を作っています..."
  "$PY" -m venv .venv || fail
fi
if [ ! -f "config.yaml" ] && [ -f "config.example.yaml" ]; then
  cp "config.example.yaml" "config.yaml"
fi
echo "[2/3] 必要な部品をインストールしています（数分かかります）..."
.venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt || fail
echo "[3/3] 収集用のブラウザをインストールしています（数分かかります）..."
.venv/bin/python -m playwright install chromium || fail
chmod +x start_gui.command 2>/dev/null

echo
echo "セットアップが終わりました。start_gui.command をダブルクリックすると画面が開きます。"
pause
exit 0
