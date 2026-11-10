"""
GoodMorningKOITA 창 모드 실행 (CMD 창 없이)

GoodMorningKOITA.vbs를 더블클릭하면 pythonw(콘솔 없는 파이썬)로 이 파일을 부른다.
- 서버를 켜고 크롬에 프로그램 화면(탭)을 연다
- 프로그램 창(첫 화면·DB 편집·DB 분석)을 모두 닫으면 잠시 뒤 서버도 스스로 끝난다
- 화면에 안 보이는 로그는 DATA_DIR(기본: 프로그램 폴더)의 goodmorning.log에 남는다
- 이미 켜져 있으면 새로 켜지 않고 창만 연다. 다른 버전이 켜져 있으면 알려 주고 멈춘다
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


def main():
    open_log()
    version = (BASE_DIR / "VERSION").read_text(encoding="utf-8").strip()
    port = int(os.getenv("PORT", "").strip() or 8000)
    url = f"http://127.0.0.1:{port}"
    other = running_version(port)
    if other == version:
        window.open_window(url)  # 이미 켜져 있음 → 창만 하나 더
        return
    if other is not None:
        window.message_box(
            (f"다른 버전(v{other})의 GoodMorningKOITA가 이미 켜져 있습니다.\n" if other else
             f"다른 프로그램이 {port}번 포트를 쓰고 있습니다.\n") +
            "그 프로그램 창을 모두 닫고 30초쯤 뒤에 다시 실행하세요.\n"
            "(예전 CMD 창이 열려 있으면 그 창도 닫으세요.)", error=True)
        return
    try:
        import app
    except ImportError as exc:
        traceback.print_exc()
        window.message_box(
            f"필요한 라이브러리가 없습니다 ({exc.name}).\nrun.bat을 한 번 실행해 설치한 뒤 다시 실행하세요.", error=True)
        return
    app.serve(window_mode=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        window.message_box(f"GoodMorningKOITA를 켜지 못했습니다.\n자세한 내용: {LOG_PATH}", error=True)
