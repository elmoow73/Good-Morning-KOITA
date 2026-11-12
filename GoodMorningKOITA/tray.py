"""
트레이 모드 (v1.5.0): 작업표시줄 오른쪽 아래 숨겨진 아이콘(시스템 트레이)에 상주한다.

- 프로그램 창(크롬 탭)을 닫아도 꺼지지 않는다. 끄려면 트레이 아이콘 → [종료]
- 트레이 메뉴: 열기(아이콘 더블클릭), 로그 보기, Windows 시작 시 자동 실행(켜기/끄기), 종료
- Windows 시작 시 자동 실행: 현재 사용자 시작프로그램(레지스트리 HKCU\\...\\Run)에 GoodMorningKOITA.vbs --background를
  등록한다 (관리자 권한 필요 없음). .env AUTOSTART=0이면 등록하지 않고 지운다 (기본: 등록).
  새 버전을 다른 폴더에 풀어 실행하면 그 폴더로 등록이 바뀐다.
- pystray·Pillow가 없거나 윈도우가 아니면 트레이를 쓸 수 없다 → launcher.py가 예전 창 모드로 돈다
"""

import os
import sys
import threading
from pathlib import Path

BASE_DIR = Path(__file__).parent
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "GoodMorningKOITA"
ICON_PATH = BASE_DIR / "static" / "icons" / "icon-192.png"


def available():
    """트레이를 쓸 수 있는지 (윈도우 + pystray·Pillow 설치)"""
    if sys.platform != "win32":
        return False
    try:
        import pystray  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError:
        return False
    return True


# ---------------- Windows 시작 시 자동 실행 ----------------

def autostart_command():
    vbs = BASE_DIR / "GoodMorningKOITA.vbs"
    return f'wscript.exe "{vbs}" --background'


def autostart_registered():
    """지금 등록된 명령 (없으면 None)"""
    if sys.platform != "win32":
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return winreg.QueryValueEx(key, RUN_NAME)[0]
    except OSError:
        return None


def set_autostart(on):
    """자동 실행 등록(on=True)·해제. 반환: 성공 여부"""
    if sys.platform != "win32":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if on:
                winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, autostart_command())
            else:
                try:
                    winreg.DeleteValue(key, RUN_NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError as exc:
        print(f"Windows 시작 시 자동 실행 설정 실패: {exc}", flush=True)
        return False


def autostart_wanted():
    return os.getenv("AUTOSTART", "").strip() != "0"


def sync_autostart():
    """.env AUTOSTART에 맞춰 등록을 맞춘다. 등록돼 있는데 다른 폴더(예전 버전)를 가리키면 이 폴더로 바꾼다"""
    if autostart_wanted():
        if autostart_registered() != autostart_command():
            set_autostart(True)
    elif autostart_registered() is not None:
        set_autostart(False)


def save_env_value(name, value):
    """.env의 name=값 줄을 바꾸거나 더한다 (트레이 메뉴에서 자동 실행을 바꿨을 때 다음 실행에도 유지되게)"""
    path = BASE_DIR / ".env"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    out, done = [], False
    for line in lines:
        if line.split("=", 1)[0].strip() == name:
            out.append(f"{name}={value}")
            done = True
        else:
            out.append(line)
    if not done:
        out += ["# Windows 시작 시 자동 실행 (1 = 켬, 0 = 끔). 트레이 메뉴에서도 바꿀 수 있음", f"{name}={value}"]
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.environ[name] = value


# ---------------- 트레이 아이콘 ----------------

def run(url, server, log_path, can_exit, open_window):
    """
    트레이 아이콘을 띄우고, [종료]를 누를 때까지 돈다 (메인 스레드에서 부른다. 서버는 다른 스레드에서 돌고 있어야 함).
    server: uvicorn.Server. can_exit(): 검색·DB 저장 중이 아니면 True. open_window(url): 프로그램 창 열기
    """
    import pystray
    from PIL import Image

    def on_open(icon, item):
        open_window(url)

    def on_log(icon, item):
        if Path(log_path).exists():
            os.startfile(log_path)  # noqa: S606

    def on_autostart(icon, item):
        on = autostart_registered() is None
        if set_autostart(on):
            save_env_value("AUTOSTART", "1" if on else "0")

    def on_exit(icon, item):
        def stop():
            import time
            if not can_exit():
                icon.notify("검색이나 DB 저장이 끝나면 종료합니다.", "GoodMorningKOITA")
            while not can_exit():
                time.sleep(1)
            server.should_exit = True
            icon.stop()
        threading.Thread(target=stop, daemon=True).start()

    menu = pystray.Menu(
        pystray.MenuItem("열기", on_open, default=True),
        pystray.MenuItem("로그 보기", on_log),
        pystray.MenuItem("Windows 시작 시 자동 실행", on_autostart,
                         checked=lambda item: autostart_registered() is not None),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("종료", on_exit),
    )
    version = (BASE_DIR / "VERSION").read_text(encoding="utf-8").strip()
    icon = pystray.Icon(RUN_NAME, Image.open(ICON_PATH), f"GoodMorningKOITA v{version}", menu)

    def watch_server():
        # 서버가 스스로 끝나면(포트 오류 등) 트레이 아이콘도 내린다
        import time
        while not getattr(server, "_gm_stopped", False):
            time.sleep(1)
        icon.stop()

    threading.Thread(target=watch_server, daemon=True).start()
    icon.run()
