"""pytest共通設定。tests/配下からscripts/直下のモジュールを素のモジュール名でimportできるようにする。"""
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
