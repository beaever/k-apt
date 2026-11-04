"""실행기의 업데이트 적용 로직 self-check. 부품 설치 없이 단독 실행 가능: `python tests/test_launcher.py`"""

import os
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import launcher

FILES = {name: "new" for name in (*launcher.REQUIRED_FILES, "start.command", "backend/app/new.py")}


def _make_zip(path: str, files: dict[str, str]) -> None:
    with zipfile.ZipFile(path, "w") as z:
        for name, content in files.items():
            z.writestr(f"k-apt-v2/{name}", content)


def _write(root: str, name: str, content: str = "old") -> str:
    path = os.path.join(root, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)
    return path


def _read(path: str) -> str:
    with open(path) as f:
        return f.read()


def test_apply_release_replaces_code_and_keeps_user_data():
    with tempfile.TemporaryDirectory() as tmp:
        root, zip_path = os.path.join(tmp, "app"), os.path.join(tmp, "r.zip")
        _make_zip(zip_path, FILES)
        main_py = _write(root, "backend/app/main.py")
        removed = _write(root, "backend/app/removed.py")
        venv_file = _write(root, "backend/venv/keep.txt")
        result_file = _write(root, "backend/app/storage/job/result.xlsx")
        unknown = _write(root, "내 메모.txt")
        state = {"version": "v1", "files": ["backend/app/main.py", "backend/app/removed.py"]}

        launcher.apply_release(zip_path, root, "v2", state)

        assert _read(main_py) == "new"
        assert os.path.exists(os.path.join(root, "backend/app/new.py"))
        assert not os.path.exists(removed), "새 버전에서 사라진 파일은 지워져야 함"
        for kept in (venv_file, result_file, unknown):
            assert _read(kept) == "old", kept
        assert state["version"] == "v2" and "backend/app/new.py" in state["files"]
        if os.name != "nt":
            assert os.access(os.path.join(root, "start.command"), os.X_OK)


def test_apply_release_rejects_wrong_zip_without_touching_files():
    with tempfile.TemporaryDirectory() as tmp:
        root, zip_path = os.path.join(tmp, "app"), os.path.join(tmp, "r.zip")
        _make_zip(zip_path, {"README.md": "x"})
        main_py = _write(root, "backend/app/main.py")
        state = {"version": "v1"}
        try:
            launcher.apply_release(zip_path, root, "v2", state)
        except ValueError:
            assert _read(main_py) == "old" and state == {"version": "v1"}
            return
        raise AssertionError("잘못된 압축 파일이 적용되면 안 됨")


def test_failed_update_keeps_current_version():
    original = launcher.curl, launcher.os.path.exists
    def fail(*args, **kwargs):
        raise OSError("인터넷 없음")
    launcher.curl = fail
    launcher.os.path.exists = lambda path: False  # 개발 폴더(.git)여도 업데이트 경로를 타게 함
    try:
        state = {"version": "v1"}
        assert launcher.update(state) is False and state == {"version": "v1"}
    finally:
        launcher.curl, launcher.os.path.exists = original


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"OK: {t.__name__}")
    print(f"모두 통과 ({len(tests)}개)")
