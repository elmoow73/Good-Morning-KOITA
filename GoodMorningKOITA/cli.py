"""
GoodMorningKOITA 명령줄 실행 (화면 없이, AI 에이전트·예약 작업용)

결과는 항상 표준 출력에 JSON 한 덩어리로 나온다. 종료 코드: 0 = 정상(문제 없음), 1 = 확인할 문제가 있음, 2 = 실행 오류

사용법 (프로그램 폴더에서, run.bat이 만든 .venv의 파이썬으로):
  python cli.py print-check [--folder 폴더] [--apply] [--fix-published] [--year 2026]
      지면 기사 파일명(.env NEWSPAPER_DIR 또는 --folder)과 기사 DB 지면 행의 날짜·지면을 비교.
      --apply: 틀린 날짜·지면을 파일명 값으로 DB(SQLite·CSV·JSON)에 바로 고친다
      --fix-published: 게재일시가 지면 날짜와 다른 지면 행(published_diff)을 지면 날짜 00:00으로 고친다
  python cli.py archive-rows [--year 2026]
      기사 DB 전체 행을 JSON으로 (연도별 DB koita_news_2026 …, --year 없으면 모든 연도)
  python cli.py stats [--from 2026-01-01] [--to 2026-12-31]
      기사 DB 분석 (총·온라인·지면·방송 건수, 년도별·월별·분류별·언론사별 + 보도자료별). DB 행 수 기준, 기간은 '날짜' 열
  python cli.py press-releases [--folder 폴더] [--from 2026-01-01] [--to 2026-12-31]
      보도자료 목록(.env PRESSRELEASE_DIR 파일명)과 보도자료별 연결 기사·건수, 미연결 [보도자료] 기사. 기간은 배포일
      종료 코드 1 = 미연결 기사·읽지 못한 파일·DB에서 사라진 연결 기사가 있음
  python cli.py press-release-candidates --id 보도자료ID
      보도자료 1건의 연결 후보 (분류 '보도자료'이고 아직 연결 안 된 기사. near = 배포일 -1~+7일, 제목 비슷한 순)
  python cli.py press-release-link --id 보도자료ID --key 기사키 [--key 기사키 ...]
      기사를 보도자료에 연결 (기사키 = 후보·미연결 목록의 key). 기사 하나는 보도자료 하나에만
  python cli.py press-release-unlink --key 기사키 [--key ...]
      연결 끊기
"""

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

import archive  # noqa: E402
import pressrelease  # noqa: E402
import printcheck  # noqa: E402
import stats  # noqa: E402


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
    # 비교할 연도 DB: --year, 없으면 모든 연도. 연번은 연도마다 1부터라 "2026:12"처럼 연도를 붙여 구분한다
    chosen = [args.year] if args.year else archive.years()
    datas = {y: archive.editor_data(y) for y in chosen}
    rows = [r | {"seq": f"{y}:{r['seq']}"} for y, d in datas.items() for r in d["rows"]]
    result = printcheck.compare(names, rows, printcheck.load_links()) | {"source": folder, "years": chosen}
    applied = published_fixed = 0
    by_seq = {f"{y}:{r['seq']}": r for y, d in datas.items() for r in d["rows"]}
    if args.apply:
        for f in result["files"]:
            if f["status"] == "mismatch" and f["row"] and f["fix"]:
                by_seq[f["row"]["seq"]].update(f["fix"])
                applied += 1
    current = [r | {"seq": key} for key, r in by_seq.items()]  # 날짜를 고친 뒤의 값
    if args.fix_published:
        for d in printcheck.published_diff(current):
            by_seq[d["seq"]]["published_at"] = d["fix"]
            published_fixed += 1
    result["published_diff"] = [] if published_fixed else printcheck.published_diff(current)
    if applied or published_fixed:
        result["saved"] = {}
        try:
            for y, d in datas.items():
                result["saved"][str(y)] = archive.replace_all(d["rows"], d["revision"], y)
        except archive.EditError as exc:
            return out({"ok": False, "error": str(exc), "problems": exc.problems}, 2)
    result["applied"] = applied
    result["published_fixed"] = published_fixed
    s = result["summary"]
    problems = s["mismatch"] + s["parse_error"] + s["no_match"] + s["duplicate"]
    result["ok"] = problems == 0 or (args.apply and s["parse_error"] + s["no_match"] + s["duplicate"] == 0)
    return out(result, 0 if result["ok"] else 1)


def archive_rows(args):
    rows = archive.all_rows(args.year)
    return out({"ok": True, "year": args.year, "years": archive.years(), "rows": rows}, 0)


def stats_cmd(args):
    rows = archive.all_rows()
    result = stats.summarize(rows, args.date_from, args.date_to)
    result["press_releases"] = pressrelease.stats_section(args.date_from, args.date_to, rows)
    return out({"ok": True} | result, 0)


def press_releases_cmd(args):
    data = pressrelease.overview(args.folder, None, None, args.date_from, args.date_to)
    s = data["summary"]
    data["ok"] = not (s["unlinked"] or s["missing_articles"] or data["file_errors"])
    return out(data, 0 if data["ok"] else 1)


def find_release(release_id, folder=None):
    """보도자료ID → 보도자료 (폴더 파일 또는 연결표에서)"""
    for r in pressrelease.overview(folder)["releases"]:
        if r["id"] == release_id:
            return r
    return None


def press_release_candidates_cmd(args):
    release = find_release(args.id, args.folder)
    if release is None:
        return out({"ok": False, "error": f"보도자료를 찾을 수 없습니다: {args.id}"}, 2)
    return out({"ok": True, "release": release} | pressrelease.candidates(release), 0)


def press_release_link_cmd(args):
    release = find_release(args.id, args.folder)
    if release is None:
        return out({"ok": False, "error": f"보도자료를 찾을 수 없습니다: {args.id}"}, 2)
    try:
        return out({"ok": True} | pressrelease.link(release, args.key), 0)
    except pressrelease.LinkError as exc:
        return out({"ok": False, "error": str(exc)}, 1)


def press_release_unlink_cmd(args):
    try:
        return out({"ok": True} | pressrelease.unlink(args.key), 0)
    except pressrelease.LinkError as exc:
        return out({"ok": False, "error": str(exc)}, 1)


def main(argv=None):
    parser = argparse.ArgumentParser(description="GoodMorningKOITA 명령줄 실행 (JSON 출력)")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("print-check", help="지면 기사 파일명과 기사 DB 지면 날짜 비교")
    p.add_argument("--folder", help="지면 기사 폴더 (기본: .env NEWSPAPER_DIR)")
    p.add_argument("--apply", action="store_true", help="틀린 날짜·지면을 DB에 바로 고치기")
    p.add_argument("--fix-published", action="store_true", help="지면 행 게재일시를 지면 날짜 00:00으로 고치기")
    p.add_argument("--year", type=int, help="비교할 연도 DB (기본: 모든 연도)")
    p.set_defaults(run=print_check)
    p = sub.add_parser("archive-rows", help="기사 DB 전체 행 출력")
    p.add_argument("--year", type=int, help="연도 DB (기본: 모든 연도)")
    p.set_defaults(run=archive_rows)
    p = sub.add_parser("stats", help="기사 DB 분석 (건수 집계)")
    p.add_argument("--from", dest="date_from", help="시작 날짜 YYYY-MM-DD (포함)")
    p.add_argument("--to", dest="date_to", help="끝 날짜 YYYY-MM-DD (포함)")
    p.set_defaults(run=stats_cmd)
    p = sub.add_parser("press-releases", help="보도자료별 연결 기사·건수")
    p.add_argument("--folder", help="보도자료 폴더 (기본: .env PRESSRELEASE_DIR)")
    p.add_argument("--from", dest="date_from", help="배포일 시작 YYYY-MM-DD (포함)")
    p.add_argument("--to", dest="date_to", help="배포일 끝 YYYY-MM-DD (포함)")
    p.set_defaults(run=press_releases_cmd)
    p = sub.add_parser("press-release-candidates", help="보도자료 1건의 연결 후보")
    p.add_argument("--id", required=True, help="보도자료ID (press-releases 결과의 id)")
    p.add_argument("--folder", help="보도자료 폴더 (기본: .env PRESSRELEASE_DIR)")
    p.set_defaults(run=press_release_candidates_cmd)
    p = sub.add_parser("press-release-link", help="기사를 보도자료에 연결")
    p.add_argument("--id", required=True, help="보도자료ID")
    p.add_argument("--key", action="append", required=True, help="기사키 (여러 번 가능)")
    p.add_argument("--folder", help="보도자료 폴더 (기본: .env PRESSRELEASE_DIR)")
    p.set_defaults(run=press_release_link_cmd)
    p = sub.add_parser("press-release-unlink", help="보도자료 연결 끊기")
    p.add_argument("--key", action="append", required=True, help="기사키 (여러 번 가능)")
    p.set_defaults(run=press_release_unlink_cmd)
    args = parser.parse_args(argv)
    try:
        return args.run(args)
    except Exception as exc:  # 에이전트가 늘 JSON을 받도록
        return out({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 2)


if __name__ == "__main__":
    sys.exit(main())
