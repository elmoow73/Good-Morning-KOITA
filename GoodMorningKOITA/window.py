"""
창 모드 도우미: CMD 창 없이 실행할 때 프로그램 창(엣지·크롬 앱 창)을 열고, 알림 상자를 띄운다.
GoodMorningKOITA.vbs → launcher.py(pythonw) → app.serve(window=True)에서 쓴다.
"""

import os
import subprocess
import sys
import webbrowser
from pathlib import Path


def find_browser():
    """주소창 없는 앱 창(--app)을 열 수 있는 브라우저 (엣지 먼저, 없으면 크롬). 없으면 None"""
    if sys.platform != "win32":
        return None
    roots = [os.getenv("ProgramFiles(x86)"), os.getenv("ProgramFiles"), os.getenv("LOCALAPPDATA")]
    for rel in (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"):
        for root in roots:
            if root and (Path(root) / rel).is_file():
                return str(Path(root) / rel)
    return None


def open_window(url):
    """프로그램 창 열기. 엣지·크롬이 있으면 앱 창, 없으면 기본 브라우저 새 창"""
    exe = find_browser()
    if exe:
        try:
            subprocess.Popen([exe, f"--app={url}", "--window-size=1400,900"], close_fds=True)
            return
        except OSError:
            pass
    webbrowser.open(url, new=1)


def message_box(text, error=False):
    """윈도우 알림 상자 (CMD 창이 없으니 문제를 이걸로 알린다). 윈도우가 아니면 표준 오류로"""
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text, "GoodMorningKOITA", 0x10 if error else 0x40)
    else:
        print(text, file=sys.stderr)
