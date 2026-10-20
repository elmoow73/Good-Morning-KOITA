"""
노션으로 기사 보내기

.env에 NOTION_TOKEN(노션 통합 시크릿)과 NOTION_DATABASE_ID(노션 페이지나 데이터베이스 링크)를
넣으면, 수집한 기사가 엑셀과 같은 형식(날짜·언론사명·구분·제목·URL)으로 노션 표에도 올라간다.
- 데이터베이스 링크면 그 표에, 일반 페이지 링크면 그 페이지 안에 "산기협 뉴스" 표를 만들어 올린다.
- 엑셀처럼 지면 기사는 지면 행(다음 날짜, URL "-"), 영상 기사는 방송 행이 따로 붙는다.
둘 다 비어 있으면 아무 일도 하지 않는다.
"""

import os
import re
import time

import requests

import exporter

API_URL = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# 엑셀과 같은 열. 제목 열은 표에 원래 있는 것을 쓰고(새로 만들 때는 "제목"), 나머지는 없으면 만든다.
PROPERTIES = {
    "날짜": {"date": {}},
    "언론사명": {"rich_text": {}},
    "구분": {"select": {}},
    "URL": {"url": {}},
}
# NOTION_DATABASE_ID에 일반 페이지 링크를 넣으면 그 페이지 안에 이 이름으로 표를 만든다
DATABASE_TITLE = "산기협 뉴스"


class NotionError(Exception):
    pass


def enabled():
    return bool(os.getenv("NOTION_TOKEN", "").strip() and os.getenv("NOTION_DATABASE_ID", "").strip())


def target_id():
    """노션 링크를 통째로 붙여넣어도 32자리 ID만 골라낸다."""
    raw = os.getenv("NOTION_DATABASE_ID", "").strip()
    last_part = raw.split("?")[0].split("#")[0].rstrip("/").split("/")[-1].replace("-", "")
    found = re.search(r"[0-9a-fA-F]{32}$", last_part)
    return found.group(0) if found else raw


class Client:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {os.getenv('NOTION_TOKEN', '').strip()}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        })
        self.database_id = None
        self.title_property = None

    def request(self, method, path, body=None, allow_missing=False):
        # 노션은 초당 3회 정도로 제한하므로 429가 오면 잠시 기다렸다 다시 보낸다
        for _ in range(4):
            response = self.session.request(method, API_URL + path, json=body, timeout=20)
            if response.status_code == 429:
                time.sleep(float(response.headers.get("Retry-After", 1)))
                continue
            if response.ok:
                return response.json()
            if allow_missing and response.status_code in (400, 404):
                return None
            raise NotionError(error_message(response))
        raise NotionError("노션 요청이 너무 많아 잠시 후 다시 시도해야 합니다.")

    def prepare_database(self):
        """올릴 표를 찾고(없으면 페이지 안에 만들고), 빠진 열을 추가한다."""
        if self.database_id:
            return
        target = target_id()
        database = self.request("GET", f"/databases/{target}", allow_missing=True)
        if database is None:
            # 데이터베이스가 아니면 일반 페이지로 보고, 그 안의 "산기협 뉴스" 표를 찾거나 만든다
            self.request("GET", f"/pages/{target}")
            database = self.find_child_database(target) or self.create_database(target)
        self.database_id = database["id"]
        existing = database.get("properties", {})
        self.title_property = next(name for name, p in existing.items() if p.get("type") == "title")
        missing = {name: spec for name, spec in PROPERTIES.items() if name not in existing}
        if missing:
            self.request("PATCH", f"/databases/{self.database_id}", {"properties": missing})

    def find_child_database(self, page_id):
        cursor = None
        while True:
            query = f"?page_size=100" + (f"&start_cursor={cursor}" if cursor else "")
            children = self.request("GET", f"/blocks/{page_id}/children{query}")
            for block in children.get("results", []):
                if block.get("type") == "child_database" and block["child_database"].get("title") == DATABASE_TITLE:
                    return self.request("GET", f"/databases/{block['id']}")
            if not children.get("has_more"):
                return None
            cursor = children.get("next_cursor")

    def create_database(self, page_id):
        return self.request("POST", "/databases", {
            "parent": {"type": "page_id", "page_id": page_id},
            "title": text(DATABASE_TITLE),
            "is_inline": True,
            "properties": {"제목": {"title": {}}, **PROPERTIES},
        })

    def create_page(self, row):
        self.prepare_database()
        page = self.request("POST", "/pages", {
            "parent": {"database_id": self.database_id},
            "properties": {
                self.title_property: {"title": text(row["제목"])},
                "날짜": {"date": {"start": row["날짜"].rstrip(".").replace(".", "-")}},
                "언론사명": {"rich_text": text(row["언론사명"])},
                "구분": {"select": {"name": row["구분"].replace(",", " ")[:100]}},
                "URL": {"url": row["URL"] if row["URL"].startswith(("http://", "https://")) else None},
            },
        })
        return page["id"]

    def archive_page(self, page_id):
        # 이미 지워진 페이지는 그냥 넘어간다
        self.request("PATCH", f"/pages/{page_id}", {"archived": True}, allow_missing=True)


def text(value):
    # 노션 텍스트 칸 하나는 2,000자까지
    return [{"type": "text", "text": {"content": value[:2000]}}] if value else []


NOTION_STATUS_HINTS = {
    401: "NOTION_TOKEN이 올바른지 확인하세요.",
    403: "통합에 이 페이지의 편집 권한이 있는지 확인하세요.",
    404: "NOTION_DATABASE_ID 링크가 맞는지, 그 페이지 ⋯ 메뉴 > 연결에서 통합을 추가했는지 확인하세요.",
}


def error_message(response):
    try:
        detail = response.json().get("message", "")
    except ValueError:
        detail = response.text[:200]
    hint = NOTION_STATUS_HINTS.get(response.status_code)
    return f"노션 오류 ({response.status_code}): {detail}" + (f" → {hint}" if hint else "")


def push_articles(articles, client=None):
    """
    아직 노션에 없는 기사를 엑셀과 같은 행(온라인/방송/지면)으로 노션에 올린다.
    돌려주는 값: ([(기사 id, "노션 페이지 id들(쉼표로 구분)")], 오류 목록)
    """
    if not enabled() or not articles:
        return [], []
    client = client or Client()
    created, errors = [], []
    # 오래된 기사부터 올려야 노션 표 기본 순서(추가한 순)가 자연스럽다
    for article in sorted(articles, key=lambda a: a["published_at"]):
        page_ids = []
        try:
            for row in reversed(exporter.to_rows([article])):
                page_ids.append(client.create_page(row))
            created.append((article["id"], ",".join(page_ids)))
        except (NotionError, requests.RequestException) as e:
            is_network = isinstance(e, requests.RequestException)
            errors.append(f"노션에 연결하지 못했습니다: {e}" if is_network else str(e))
            # 반쯤 올라간 기사는 지우고 다음에 통째로 다시 올린다
            for page_id in page_ids:
                try:
                    client.archive_page(page_id)
                except (NotionError, requests.RequestException):
                    pass
            # 연결·토큰·권한 문제면 나머지도 똑같이 실패하므로 멈춘다
            if is_network or re.search(r"\((400|401|403|404)\)", str(e)):
                break
    return created, errors


def archive_article(page_ids):
    if enabled() and page_ids:
        client = Client()
        for page_id in page_ids.split(","):
            client.archive_page(page_id)


def replace_article(page_ids, article):
    """언론사명·지면·영상을 고치면 행 구성이 바뀔 수 있으므로, 예전 행을 지우고 새로 올린다."""
    if not (enabled() and page_ids):
        return None
    client = Client()
    for page_id in page_ids.split(","):
        client.archive_page(page_id)
    created, errors = push_articles([article], client)
    if errors:
        raise NotionError(errors[0])
    return created[0][1]
