"""
노션 데이터베이스로 기사 보내기

.env에 NOTION_TOKEN(노션 통합 토큰)과 NOTION_DATABASE_ID(기사를 쌓을 데이터베이스)를
넣으면, 수집한 기사가 노션 데이터베이스에도 한 줄씩 추가된다. 둘 다 비어 있으면 아무 일도 하지 않는다.
"""

import os
import re
import time

import requests

API_URL = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# 데이터베이스에 없으면 자동으로 만드는 열. 제목 열은 데이터베이스에 원래 있는 것을 그대로 쓴다.
PROPERTIES = {
    "날짜": {"date": {}},
    "언론사": {"rich_text": {}},
    "지면": {"rich_text": {}},
    "영상": {"checkbox": {}},
    "URL": {"url": {}},
    "키워드": {"multi_select": {}},
    "요약": {"rich_text": {}},
}


class NotionError(Exception):
    pass


def enabled():
    return bool(os.getenv("NOTION_TOKEN", "").strip() and os.getenv("NOTION_DATABASE_ID", "").strip())


def database_id():
    """노션 링크를 통째로 붙여넣어도 32자리 ID만 골라낸다."""
    raw = os.getenv("NOTION_DATABASE_ID", "").strip()
    last_part = raw.split("?")[0].rstrip("/").split("/")[-1].replace("-", "")
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
        self.database_id = database_id()
        self.title_property = None

    def request(self, method, path, body=None):
        # 노션은 초당 3회 정도로 제한하므로 429가 오면 잠시 기다렸다 다시 보낸다
        for _ in range(4):
            response = self.session.request(method, API_URL + path, json=body, timeout=20)
            if response.status_code == 429:
                time.sleep(float(response.headers.get("Retry-After", 1)))
                continue
            if response.ok:
                return response.json()
            raise NotionError(error_message(response))
        raise NotionError("노션 요청이 너무 많아 잠시 후 다시 시도해야 합니다.")

    def prepare_database(self):
        """제목 열 이름을 알아내고, 빠진 열(날짜·언론사 등)을 데이터베이스에 추가한다."""
        if self.title_property:
            return
        database = self.request("GET", f"/databases/{self.database_id}")
        existing = database.get("properties", {})
        self.title_property = next(name for name, p in existing.items() if p.get("type") == "title")
        missing = {name: spec for name, spec in PROPERTIES.items() if name not in existing}
        if missing:
            self.request("PATCH", f"/databases/{self.database_id}", {"properties": missing})

    def page_properties(self, article):
        return {
            self.title_property: {"title": text(article["title"])},
            "날짜": {"date": {"start": article["published_at"]}},
            "언론사": {"rich_text": text(article.get("press") or "")},
            "지면": {"rich_text": text(article.get("print_page") or "온라인")},
            "영상": {"checkbox": bool(article.get("has_video"))},
            "URL": {"url": article.get("url") or None},
            "키워드": {"multi_select": [
                {"name": k.strip().replace(",", " ")[:100]}
                for k in (article.get("keywords") or "").split(", ") if k.strip()
            ]},
            "요약": {"rich_text": text(article.get("description") or "")},
        }

    def create_page(self, article):
        self.prepare_database()
        page = self.request("POST", "/pages", {
            "parent": {"database_id": self.database_id},
            "properties": self.page_properties(article),
        })
        return page["id"]

    def update_page(self, page_id, article):
        self.prepare_database()
        self.request("PATCH", f"/pages/{page_id}", {"properties": self.page_properties(article)})

    def archive_page(self, page_id):
        self.request("PATCH", f"/pages/{page_id}", {"archived": True})


def text(value):
    # 노션 텍스트 칸 하나는 2,000자까지
    return [{"type": "text", "text": {"content": value[:2000]}}] if value else []


NOTION_STATUS_HINTS = {
    401: "NOTION_TOKEN이 올바른지 확인하세요.",
    403: "통합에 이 데이터베이스의 편집 권한이 있는지 확인하세요.",
    404: "NOTION_DATABASE_ID가 맞는지, 데이터베이스 ⋯ 메뉴 > 연결에서 통합을 추가했는지 확인하세요.",
}


def error_message(response):
    try:
        detail = response.json().get("message", "")
    except ValueError:
        detail = response.text[:200]
    hint = NOTION_STATUS_HINTS.get(response.status_code)
    return f"노션 오류 ({response.status_code}): {detail}" + (f" → {hint}" if hint else "")


def push_articles(articles):
    """
    아직 노션에 없는 기사를 노션 데이터베이스에 추가한다.
    돌려주는 값: ([(기사 id, 노션 페이지 id)], 오류 목록)
    """
    if not enabled() or not articles:
        return [], []
    client = Client()
    created, errors = [], []
    for article in articles:
        try:
            created.append((article["id"], client.create_page(article)))
        except NotionError as e:
            errors.append(str(e))
            # 토큰·권한 문제면 나머지도 똑같이 실패하므로 멈춘다
            if re.search(r"\((400|401|403|404)\)", str(e)):
                break
        except requests.RequestException as e:
            errors.append(f"노션에 연결하지 못했습니다: {e}")
            break
    return created, errors


def update_article(page_id, article):
    if enabled() and page_id:
        Client().update_page(page_id, article)


def archive_article(page_id):
    if enabled() and page_id:
        Client().archive_page(page_id)
