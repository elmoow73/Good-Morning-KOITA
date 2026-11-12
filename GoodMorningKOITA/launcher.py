"""
GoodMorningKOITA 창 모드 실행 (CMD 창 없이)

GoodMorningKOITA.vbs를 더블클릭하면 pythonw(콘솔 없는 파이썬)로 이 파일을 부른다.
- 서버를 켜고 크롬에 프로그램 화면(탭)을 연다
- v1.5.0: 작업표시줄 숨겨진 아이콘(트레이)에 상주한다. 창을 닫아도 꺼지지 않고, 트레이 아이콘 → [종료]로 끈다
  (tray.py. pystray가 없으면 예전처럼 프로그램 창을 모두 닫으면 잠시 뒤 서버도 끝난다)
- --background: Windows 시작 시 자동 실행용. 창을 열지 않고 트레이에만 둔다
- 화면에 안 보이는 로그는 DATA_DIR(기본: 프로그램 폴더)의 goodmorning.log에 남는다
- 이미 켜져 있으면 새로 켜지 않고 창만 연다 (두 번 빨리 눌러도 하나만 켜짐). 다른 버전이 켜져 있으면 알려 주고 멈춘다
CMD 창을 보며 실행하려면(문제 확인용) run.bat을 쓴다.
"""

import json
import os
import sys
import traceback
import urllib.error
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).parent
os.chdir(BASE_DIR)
sys.path.insert(0, str(BASE_DIR))

import window  # noqa: E402  (표준 라이브러리만 씀)

try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
except ImportError:
    pass  # 라이브러리가 없으면 아래 app import에서 알린다

LOG_PATH = Path(os.getenv("DATA_DIR", "").strip() or BASE_DIR) / "goodmorning.log"


def open_log():
    """pythonw는 출력할 곳이 없으므로 로그 파일로 돌린다 (2MB 넘으면 .old로 넘김)"""
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if LOG_PATH.exists() and LOG_PATH.stat().st_size > 2_000_000:
        LOG_PATH.replace(LOG_PATH.with_suffix(".log.old"))
    log = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = log


def running_version(port):
    """이 PC에서 이미 켜진 GoodMorningKOITA의 버전. 안 켜져 있으면 None, 다른 프로그램이 쓰는 포트면 ''"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as res:
            return json.loads(res.read().decode("utf-8")).get("version", "")
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8")).get("version", "")
        except ValueError:
            return ""
    except (urllib.error.URLError, OSError):
        return None
    except ValueError:
        return ""


def single_instance_lock():
    """
    이 PC에서 GoodMorningKOITA 실행기가 하나만 돌게 하는 윈도우 이름 있는 뮤텍스.
    반환: 잡았으면 핸들, 이미 다른 실행기가 잡고 있으면 None (윈도우가 아니면 늘 True)
    """
    if sys.platform != "win32":
        return True
    import ctypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.CreateMutexW(None, False, "Local\\GoodMorningKOITA")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return None
    return handle


def wait_running(port, seconds=40):
    """다른 실행기가 서버를 켜는 중일 때 켜질 때까지 기다린다. 반환: 버전 또는 None"""
    import time
    for _ in range(seconds):
        found = running_version(port)
        if found is not None:
            return found
        time.sleep(1)
    return None


def main():
    open_log()
    background = "--background" in sys.argv[1:]
    version = (BASE_DIR / "VERSION").read_text(encoding="utf-8").strip()
    port = int(os.getenv("PORT", "").strip() or 8000)
    url = f"http://127.0.0.1:{port}"
    lock = single_instance_lock()
    other = running_version(port)
    if lock is None and other is None:
        other = wait_running(port)  # 방금 다른 실행기가 켜는 중 (두 번 빨리 누름·자동 실행과 겹침)
        if other is None:
            return
    if other == version:
        if not background:
            window.open_window(url)  # 이미 켜져 있음 → 새로 켜지 않고 창만 연다
        return
    if other is not None:
        if background:
            return  # 자동 실행 때는 조용히 넘어간다
        window.message_box(
            (f"다른 버전(v{other})의 GoodMorningKOITA가 이미 켜져 있습니다.\n" if other else
             f"다른 프로그램이 {port}번 포트를 쓰고 있습니다.\n") +
            "작업표시줄 오른쪽 아래 숨겨진 아이콘(^)에서 GoodMorningKOITA를 우클릭해 [종료]한 뒤 다시 실행하세요.\n"
            "(예전 버전이면 그 프로그램 창을 모두 닫고 30초쯤 뒤에, 예전 CMD 창이 열려 있으면 그 창도 닫으세요.)", error=True)
        return
    try:
        import app
    except ImportError as exc:
        traceback.print_exc()
        window.message_box(
            f"필요한 라이브러리가 없습니다 ({exc.name}).\nrun.bat을 한 번 실행해 설치한 뒤 다시 실행하세요.", error=True)
        return
    import tray
    if tray.available():
        app.serve(tray_mode=True, open_first=not background)
    else:
        print("트레이 아이콘을 쓸 수 없어(pystray 없음) 창 모드로 실행합니다. run.bat --setup을 한 번 실행하세요.", flush=True)
        app.serve(window_mode=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        window.message_box(f"GoodMorningKOITA를 켜지 못했습니다.\n자세한 내용: {LOG_PATH}", error=True)
