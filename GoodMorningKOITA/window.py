"""
창 모드 도우미: CMD 창 없이 실행할 때 프로그램 화면을 크롬 탭으로 열고, 알림 상자를 띄운다.
GoodMorningKOITA.vbs → launcher.py(pythonw) → app.serve(window=True)에서 쓴다.
"""

import os
import subprocess
import sys
import webbrowser
from pathlib import Path


def find_chrome():
    """크롬 실행 파일 (윈도우). 없으면 None"""
    if sys.platform != "win32":
        return None
    for root in (os.getenv("ProgramFiles"), os.getenv("ProgramFiles(x86)"), os.getenv("LOCALAPPDATA")):
        path = Path(root or "") / r"Google\Chrome\Application\chrome.exe"
        if root and path.is_file():
            return str(path)
    return None


def open_window(url):
    """
    프로그램 화면을 크롬 탭으로 연다 (크롬이 켜져 있으면 그 창에 새 탭, 꺼져 있으면 새 창).
    DB 편집·DB 분석도 같은 크롬에서 탭으로 열린다. 크롬이 없으면 기본 브라우저로
    """
    exe = find_chrome()
    if exe:
        try:
            subprocess.Popen([exe, url], close_fds=True)
            return
        except OSError:
            pass
    webbrowser.open(url, new=2)


def message_box(text, error=False):
    """윈도우 알림 상자 (CMD 창이 없으니 문제를 이걸로 알린다). 윈도우가 아니면 표준 오류로"""
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text, "GoodMorningKOITA", 0x10 if error else 0x40)
    else:
        print(text, file=sys.stderr)
