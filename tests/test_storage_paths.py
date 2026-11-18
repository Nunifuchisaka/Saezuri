"""storage.pyの状態ファイルのパス組み立ての単体テスト。値はすべて架空。"""

import storage


def test_last_id_path_keeps_usual_names(tmp_path):
    assert storage._last_id_path(tmp_path, "example_a").name == "example_a_last_id.json"
    assert storage._last_id_path(tmp_path, "instagram_example.b").name == "instagram_example.b_last_id.json"


def test_last_id_path_stays_inside_state_dir(tmp_path):
    for account in ("../../x", "..\\..\\x", "/abs/x", "a/b", "C:\\x", ".."):
        path = storage._last_id_path(tmp_path, account)
        assert path.parent == tmp_path, account
        assert "/" not in path.name and "\\" not in path.name


def test_last_id_roundtrip(tmp_path):
    storage.save_last_id(tmp_path, "../x", "10")
    assert storage.load_last_id(tmp_path, "../x") == "10"
    assert [p.name for p in tmp_path.iterdir()] == ["_x_last_id.json"]
