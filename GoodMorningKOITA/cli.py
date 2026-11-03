"""
GoodMorningKOITA 명령줄 실행 (화면 없이, AI 에이전트·예약 작업용)

결과는 항상 표준 출력에 JSON 한 덩어리로 나온다. 종료 코드: 0 = 정상(문제 없음), 1 = 확인할 문제가 있음, 2 = 실행 오류

사용법 (프로그램 폴더에서, run.bat이 만든 .venv의 파이썬으로):
  python cli.py print-check [--folder 폴더] [--apply]
      지면 기사 파일명(.env NEWSPAPER_DIR 또는 --folder)과 기사 DB 지면 행의 날짜·지면을 비교.
      --apply: 틀린 날짜·지면을 파일명 값으로 DB(SQLite·CSV·JSON)에 바로 고친다
  python cli.py archive-rows
      기사 DB 전체 행을 JSON으로
"""

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

import archive  # noqa: E402
import printcheck  # noqa: E402


def out(data, code):
    print(json.dumps(data, ensure_ascii=False, indent=1))
    return code


def print_check(args):
    folder = args.folder or printcheck.default_folder()
    if not folder:
        return out({"ok": False, "error": "--folder 또는 .env NEWSPAPER_DIR이 필요합니다."}, 2)
    try:
        names = printcheck.list_files(folder)
    except FileNotFoundError as exc:
        return out({"ok": False, "error": str(exc)}, 2)
    data = archive.editor_data()
    result = printcheck.compare(names, data["rows"], printcheck.load_links()) | {"source": folder}
    applied = 0
    if args.apply:
        by_seq = {r["seq"]: r for r in data["rows"]}
        for f in result["files"]:
            if f["status"] == "mismatch" and f["row"] and f["fix"]:
                by_seq[f["row"]["seq"]].update(f["fix"])
                applied += 1
        if applied:
            try:
                result["saved"] = archive.replace_all(data["rows"], data["revision"])
            except archive.EditError as exc:
                return out({"ok": False, "error": str(exc), "problems": exc.problems}, 2)
    result["applied"] = applied
    s = result["summary"]
    problems = s["mismatch"] + s["parse_error"] + s["no_match"] + s["duplicate"]
    result["ok"] = problems == 0 or (args.apply and s["parse_error"] + s["no_match"] + s["duplicate"] == 0)
    return out(result, 0 if result["ok"] else 1)


def archive_rows(_args):
    return out({"ok": True, "rows": archive.all_rows()}, 0)


def main(argv=None):
    parser = argparse.ArgumentParser(description="GoodMorningKOITA 명령줄 실행 (JSON 출력)")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("print-check", help="지면 기사 파일명과 기사 DB 지면 날짜 비교")
    p.add_argument("--folder", help="지면 기사 폴더 (기본: .env NEWSPAPER_DIR)")
    p.add_argument("--apply", action="store_true", help="틀린 날짜·지면을 DB에 바로 고치기")
    p.set_defaults(run=print_check)
    sub.add_parser("archive-rows", help="기사 DB 전체 행 출력").set_defaults(run=archive_rows)
    args = parser.parse_args(argv)
    try:
        return args.run(args)
    except Exception as exc:  # 에이전트가 늘 JSON을 받도록
        return out({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 2)


if __name__ == "__main__":
    sys.exit(main())
