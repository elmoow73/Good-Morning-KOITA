"""
지면보도 날짜 확인

지면 기사 파일(스크랩 이미지 등)의 파일명과 기사 DB의 지면 행을 비교해, DB의 날짜·지면이 틀린 곳을 찾는다.
파일명 형식: [매체명]_기사제목_지면_날짜.jpg  (예: [매일경제]_산기협 장영실상 시상_A16면_20261027.jpg)
- 매체명의 대괄호는 있어도 없어도 된다
- 제목 안에 밑줄(_)이 있어도 된다 (첫 칸 = 매체명, 끝 칸 = 날짜, 끝에서 둘째 칸 = 지면)
- 날짜는 20261027, 2026-10-27, 2026.10.27, 261027 등을 읽는다
- 지면은 A16면, A16, 16면 등을 읽는다

DB의 지면 행(구분이 온라인·방송이 아닌 행)과 매체명·제목으로 같은 기사를 찾고, 날짜·지면을 비교한다.
DB를 고치지는 않는다. 결과를 보고 편집기(또는 cli.py --apply)에서 고친다.
"""

import os
import re
import unicodedata
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path

FILE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".pdf", ".heic"}
TITLE_MATCH = 0.75       # 제목이 이만큼 비슷하면 같은 기사 후보 (매체명이 같을 때)
TITLE_MATCH_ANY = 0.9    # 매체명이 달라도 제목이 이만큼 비슷하면 후보 (매체명 표기가 다른 경우)
MISSING_FILE_MARGIN = 3  # 파일 날짜 범위에서 앞뒤 며칠까지 "파일 없음"을 보여 줄지


def default_folder():
    return os.getenv("NEWSPAPER_DIR", "").strip()


def norm(text):
    """비교용: 전각·반각 통일, 소문자, 글자·숫자만 (띄어쓰기·기호·따옴표 무시)"""
    text = unicodedata.normalize("NFKC", text or "").lower()
    return re.sub(r"[^0-9a-z가-힣]", "", text)


def parse_date(text):
    """파일명의 날짜 칸을 date로. 못 읽으면 None"""
    numbers = re.findall(r"\d+", text)
    try:
        if len(numbers) == 1 and len(numbers[0]) in (6, 8):
            digits = numbers[0]
            if len(digits) == 6:
                digits = "20" + digits
            return date(int(digits[:4]), int(digits[4:6]), int(digits[6:]))
        if len(numbers) == 3:
            year = int(numbers[0])
            year += 2000 if year < 100 else 0
            return date(year, int(numbers[1]), int(numbers[2]))
    except ValueError:
        return None
    return None


def parse_page(text):
    """'A16면 1단', 'A16', '16면' → 'A16'. 못 읽으면 None"""
    match = re.search(r"(?<![A-Za-z0-9])([A-Za-z]{0,2})\s*0*(\d{1,3})(?!\d)\s*(면|$|\s)", unicodedata.normalize("NFKC", text or ""))
    if not match:
        return None
    return f"{match.group(1).upper()}{match.group(2)}"


def parse_filename(name):
    """
    파일명 1개를 읽는다.
    반환: {"file", "press", "title", "page", "date", "errors": [{"part", "value", "message"}], "notes": [...]}
    """
    stem = Path(name).stem
    result = {"file": name, "press": "", "title": "", "page": None, "page_text": "", "date": None,
              "date_text": "", "errors": [], "notes": []}
    parts = [p.strip() for p in stem.split("_")]
    if len(parts) < 4:
        result["errors"].append({
            "part": "형식", "value": stem,
            "message": f"밑줄(_)로 나뉜 칸이 {len(parts)}개입니다. [매체명]_기사제목_지면_날짜 4칸이 필요합니다.",
        })
        return result

    result["press"] = parts[0].strip("[]【】()（）<>〈〉 ")
    page_text, date_text = parts[-2], parts[-1]
    found = parse_date(date_text)
    if found is None and parse_date(page_text) is not None and parse_page(date_text):
        # 지면과 날짜 순서가 바뀐 파일명
        page_text, date_text = date_text, page_text
        found = parse_date(date_text)
        result["notes"].append("지면과 날짜 칸 순서가 바뀌어 있어 바꿔 읽었습니다.")
    result["title"] = "_".join(parts[1:-2])
    result["page_text"], result["date_text"] = page_text, date_text
    result["date"] = found.isoformat() if found else None
    result["page"] = parse_page(page_text)

    if not result["press"]:
        result["errors"].append({"part": "매체명", "value": parts[0], "message": "매체명이 비어 있습니다."})
    if not norm(result["title"]):
        result["errors"].append({"part": "제목", "value": result["title"], "message": "기사제목이 비어 있습니다."})
    if found is None:
        result["errors"].append({"part": "날짜", "value": date_text,
                                 "message": "날짜로 읽을 수 없습니다 (예: 20261027, 2026-10-27)."})
    if result["page"] is None:
        result["notes"].append(f"지면 칸 '{page_text}'에서 면 번호를 읽지 못해 날짜만 비교했습니다.")
    return result


def list_files(folder):
    """폴더(하위 폴더 포함)의 지면 기사 파일 이름. 폴더가 없으면 FileNotFoundError"""
    root = Path(folder).expanduser()
    if not root.is_dir():
        raise FileNotFoundError(f"폴더를 찾을 수 없습니다: {folder}")
    return sorted(
        str(p.relative_to(root)) for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in FILE_EXTENSIONS
    )


def title_score(a, b):
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    short, long_ = sorted((a, b), key=len)
    # 파일명은 길이 제한·금지 문자 때문에 제목이 잘리거나 빠지는 경우가 많다
    if len(short) >= 8 and short in long_:
        return 0.95
    return SequenceMatcher(None, a, b).ratio()


def press_same(a, b):
    a, b = norm(a), norm(b)
    return bool(a and b) and (a == b or a in b or b in a)


def compare(names, rows):
    """
    names: 파일명 목록, rows: 기사 DB 행 목록 (seq, date, press, type, title ...)
    반환: {"summary", "files": [...], "missing": [...]}
    files 항목의 status: ok / mismatch / no_match / parse_error / duplicate
    """
    print_rows = [r for r in rows if r.get("type") not in ("온라인", "방송")]
    parsed = [parse_filename(Path(n).name) | {"path": n} for n in names]

    # 모든 (파일, 행) 후보 점수를 매기고, 점수가 높은 짝부터 1:1로 맞춘다
    pairs = []
    for fi, f in enumerate(parsed):
        if any(e["part"] in ("형식", "제목") for e in f["errors"]):
            continue
        for ri, r in enumerate(print_rows):
            score = title_score(f["title"], r["title"])
            same_press = press_same(f["press"], r["press"])
            if score >= TITLE_MATCH_ANY or (same_press and score >= TITLE_MATCH):
                # 같은 점수면 매체명이 같고 날짜가 가까운 행을 먼저
                gap = 999
                if f["date"]:
                    gap = abs((date.fromisoformat(f["date"]) - date.fromisoformat(r["date"])).days)
                pairs.append((-(score + (0.05 if same_press else 0)), gap, fi, ri))
    pairs.sort()
    file_to_row, row_to_file = {}, {}
    for _, _, fi, ri in pairs:
        if fi in file_to_row:
            continue
        if ri in row_to_file:
            continue
        file_to_row[fi], row_to_file[ri] = ri, fi
    candidates = {fi for _, _, fi, _ in pairs}

    files = []
    for fi, f in enumerate(parsed):
        item = {
            "file": f["path"], "press": f["press"], "title": f["title"], "page": f["page"],
            "page_text": f["page_text"], "date": f["date"], "date_text": f["date_text"],
            "notes": f["notes"], "problems": [], "row": None, "fix": {},
        }
        if f["errors"] and fi not in file_to_row:
            item["status"] = "parse_error"
            item["problems"] = [{"part": e["part"], "file": e["value"], "db": "", "message": e["message"]}
                                for e in f["errors"]]
        elif fi not in file_to_row:
            item["status"] = "duplicate" if fi in candidates else "no_match"
            item["problems"].append({
                "part": "기사", "file": f["title"], "db": "",
                "message": ("같은 기사로 보이는 다른 파일이 이미 DB 행과 짝지어졌습니다 (중복 파일?)."
                            if fi in candidates else "DB의 지면 행에서 같은 매체명·제목의 기사를 찾지 못했습니다."),
            })
        else:
            r = print_rows[file_to_row[fi]]
            item["row"] = {k: r.get(k) for k in ("seq", "date", "press", "type", "title")}
            for e in f["errors"]:
                item["problems"].append({"part": e["part"], "file": e["value"], "db": "", "message": e["message"]})
            if f["date"] and f["date"] != r["date"]:
                days = (date.fromisoformat(f["date"]) - date.fromisoformat(r["date"])).days
                item["problems"].append({
                    "part": "날짜", "file": f["date"], "db": r["date"],
                    "message": f"DB 날짜가 파일보다 {abs(days)}일 {'빠릅니다' if days > 0 else '늦습니다'}.",
                })
                item["fix"]["date"] = f["date"]
            db_page = parse_page(r["type"])
            if f["page"] and db_page and f["page"] != db_page:
                item["problems"].append({
                    "part": "지면", "file": f["page_text"], "db": r["type"],
                    "message": "지면 번호가 다릅니다.",
                })
                item["fix"]["type"] = f"{f['page']}면"
            if not press_same(f["press"], r["press"]):
                item["notes"].append(f"매체명 표기가 다릅니다 (파일 '{f['press']}', DB '{r['press']}'). 제목으로 맞췄습니다.")
            if title_score(f["title"], r["title"]) < 1:
                item["notes"].append(f"DB 제목: {r['title']}")
            item["status"] = "mismatch" if item["problems"] else "ok"
        files.append(item)

    # 파일 날짜 범위 안에서 파일이 없는 DB 지면 행
    dates = [date.fromisoformat(f["date"]) for f in parsed if f["date"]]
    missing = []
    if dates:
        start = (min(dates) - timedelta(days=MISSING_FILE_MARGIN)).isoformat()
        end = (max(dates) + timedelta(days=MISSING_FILE_MARGIN)).isoformat()
        missing = [
            {k: r.get(k) for k in ("seq", "date", "press", "type", "title")}
            for ri, r in enumerate(print_rows)
            if ri not in row_to_file and start <= r["date"] <= end
        ]

    order = {"mismatch": 0, "parse_error": 1, "no_match": 2, "duplicate": 3, "ok": 4}
    files.sort(key=lambda x: (order[x["status"]], x["date"] or "", x["file"]))
    summary = {s: sum(1 for x in files if x["status"] == s) for s in order}
    summary.update(files=len(files), print_rows=len(print_rows), missing=len(missing),
                   checked_at=datetime.now().astimezone().isoformat(timespec="seconds"))
    return {"summary": summary, "files": files, "missing": missing}
