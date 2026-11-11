"""공연 정보 소스.

- KOPIS(공연예술통합전산망) 오픈API: 국내 공연. 정부가 운영하는 공식 공연 DB.
- Ticketmaster Discovery API: 해외 공연(북미·유럽·호주 등).
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import time
import xml.etree.ElementTree as ET

import requests

log = logging.getLogger(__name__)

TIMEOUT = 20
HEADERS = {"User-Agent": "concert-watch/1.0"}


class SourceError(Exception):
    pass


def norm(text: str | None) -> str:
    """대소문자와 공백을 무시하고 비교하기 위한 문자열."""
    return "".join((text or "").lower().split())


def _excluded(title: str, artist: dict) -> bool:
    t = norm(title)
    return any(norm(x) in t for x in artist.get("exclude_keywords") or [])


# ── KOPIS ───────────────────────────────────────────────
KOPIS_BASE = os.environ.get("KOPIS_BASE_URL", "http://www.kopis.or.kr/openApi/restful")
KOPIS_GENRE_POP = "CCCD"   # 대중음악
KOPIS_MAX_SPAN = 31        # 한 번에 조회하는 최대 기간(일)
KOPIS_ROWS = 100
KOPIS_PAGE_URL = "https://www.kopis.or.kr/por/db/pblprfr/pblprfrView.do?menuId=MNU_00020&mt20Id={id}"


def _text(node, tag: str) -> str:
    el = node.find(tag)
    return el.text.strip() if el is not None and el.text else ""


def _kopis_date(value: str) -> str:
    return value.replace(".", "-") if value else ""


def parse_kopis_list(xml_bytes: bytes, artist: dict) -> list[dict]:
    root = ET.fromstring(xml_bytes)
    events = []
    for db in root.iter("db"):
        mt20id = _text(db, "mt20id")
        title = _text(db, "prfnm")
        if not mt20id or _excluded(title, artist):
            continue
        events.append({
            "key": f"kopis:{mt20id}",
            "source": "kopis",
            "artist": artist["name"],
            "title": title,
            "start": _kopis_date(_text(db, "prfpdfrom")),
            "end": _kopis_date(_text(db, "prfpdto")),
            "time": "",
            "venue": _text(db, "fcltynm"),
            "city": _text(db, "area"),
            "country": "KR",
            "url": KOPIS_PAGE_URL.format(id=mt20id),
            "ticket_links": [],
            "sale_start": "",
        })
    return events


KOPIS_PAUSE = 0.5          # 요청 사이 쉬는 시간(초). 빠르게 연달아 부르면 400으로 거절하는 경우가 있다
KOPIS_RETRIES = 3


def _kopis_get(s: requests.Session, url: str, params: dict) -> requests.Response:
    """KOPIS 요청. 잠깐 쉬었다 부르고, 거절·오류면 점점 길게 쉬며 다시 시도한다."""
    for attempt in range(KOPIS_RETRIES):
        time.sleep(KOPIS_PAUSE * (4 ** attempt))      # 0.5초, 2초, 8초
        try:
            r = s.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
        except requests.RequestException:
            if attempt == KOPIS_RETRIES - 1:
                raise
            continue
        if r.status_code < 400:
            return r
        if attempt == KOPIS_RETRIES - 1:
            raise SourceError(f"KOPIS {r.status_code}: {r.text.strip()[:200] or '응답 내용 없음'}")
    raise SourceError("KOPIS 요청 실패")


def fetch_kopis(artists: list[dict], today: dt.date, lookahead_days: int,
                api_key: str, session: requests.Session | None = None) -> list[dict]:
    s = session or requests.Session()
    end = today + dt.timedelta(days=lookahead_days)
    found: dict[str, dict] = {}
    for artist in artists:
        for kw in artist.get("kopis_keywords") or []:
            start = today
            while start <= end:
                stop = min(start + dt.timedelta(days=KOPIS_MAX_SPAN - 1), end)
                page = 1
                while True:
                    r = _kopis_get(s, f"{KOPIS_BASE}/pblprfr", {
                        "service": api_key,
                        "stdate": start.strftime("%Y%m%d"),
                        "eddate": stop.strftime("%Y%m%d"),
                        "cpage": page,
                        "rows": KOPIS_ROWS,
                        "shprfnm": kw,
                        "shcate": KOPIS_GENRE_POP,
                    })
                    if b"<dbs" not in r.content:
                        raise SourceError(f"예상과 다른 응답: {r.text[:200]}")
                    for e in parse_kopis_list(r.content, artist):
                        found.setdefault(e["key"], e)
                    if r.content.count(b"<db>") < KOPIS_ROWS:
                        break
                    page += 1
                start = stop + dt.timedelta(days=1)
    return list(found.values())


def enrich_kopis(event: dict, api_key: str, session: requests.Session | None = None) -> None:
    """새 공연에 한해 상세 정보(예매처 링크, 공연 시간)를 채운다."""
    s = session or requests.Session()
    mt20id = event["key"].split(":", 1)[1]
    r = _kopis_get(s, f"{KOPIS_BASE}/pblprfr/{mt20id}", {"service": api_key})
    root = ET.fromstring(r.content)
    links = []
    for rel in root.iter("relate"):
        url = _text(rel, "relateurl")
        if url.startswith("http"):
            links.append({"name": _text(rel, "relatenm") or "예매처", "url": url})
    if links:
        event["ticket_links"] = links
    for node in root.iter("dtguidance"):
        if node.text and node.text.strip():
            event["time"] = node.text.strip()
            break


# ── Ticketmaster ───────────────────────────────────────
TM_BASE = "https://app.ticketmaster.com/discovery/v2"


def _tm_get(s: requests.Session, path: str, params: dict, api_key: str) -> dict:
    time.sleep(0.25)  # 초당 5회 제한
    r = s.get(f"{TM_BASE}/{path}", params={**params, "apikey": api_key},
              headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def find_tm_attraction(s: requests.Session, keyword: str, api_key: str) -> str | None:
    data = _tm_get(s, "attractions.json",
                   {"keyword": keyword, "classificationName": "music", "size": 20}, api_key)
    for a in data.get("_embedded", {}).get("attractions", []):
        if norm(a.get("name")) == norm(keyword):
            return a.get("id")
    return None


def tm_sale_times(ev: dict) -> list[dict]:
    """Ticketmaster의 선예매·일반 예매 시작 시각."""
    sales = ev.get("sales", {})
    out = []
    for pre in sales.get("presales") or []:
        if pre.get("startDateTime"):
            out.append({"label": f"선예매({pre['name']})" if pre.get("name") else "선예매",
                        "at": pre["startDateTime"]})
    if (sales.get("public") or {}).get("startDateTime"):
        out.append({"label": "일반 예매", "at": sales["public"]["startDateTime"]})
    return sorted(out, key=lambda c: c["at"])


def parse_tm_event(ev: dict, artist: dict) -> dict:
    start = ev.get("dates", {}).get("start", {})
    venue = (ev.get("_embedded", {}).get("venues") or [{}])[0]
    url = ev.get("url", "")
    date = start.get("localDate", "")
    return {
        "key": f"tm:{ev['id']}",
        "source": "ticketmaster",
        "artist": artist["name"],
        "title": ev.get("name", ""),
        "start": date,
        "end": date,
        "time": (start.get("localTime") or "")[:5],
        "venue": venue.get("name", ""),
        "city": venue.get("city", {}).get("name", ""),
        "country": venue.get("country", {}).get("countryCode", ""),
        "url": url,
        "ticket_links": [{"name": "Ticketmaster", "url": url}] if url else [],
        "sale_start": ev.get("sales", {}).get("public", {}).get("startDateTime", ""),
        "sale_times": tm_sale_times(ev),
    }


def fetch_ticketmaster(artists: list[dict], today: dt.date, lookahead_days: int,
                       api_key: str, countries: list[str] | None = None,
                       session: requests.Session | None = None) -> list[dict]:
    s = session or requests.Session()
    window = {
        "startDateTime": f"{today.isoformat()}T00:00:00Z",
        "endDateTime": f"{(today + dt.timedelta(days=lookahead_days)).isoformat()}T23:59:59Z",
    }
    found: dict[str, dict] = {}
    for artist in artists:
        kw = artist.get("ticketmaster_keyword")
        aid = artist.get("ticketmaster_attraction_id")
        if not aid and kw:
            aid = find_tm_attraction(s, kw, api_key)
            if not aid:
                log.warning("Ticketmaster에서 '%s'와 이름이 정확히 같은 아티스트를 찾지 못했어요", kw)
        if not aid:
            continue
        for cc in countries or [None]:
            page = 0
            while True:
                params = {"attractionId": aid, "size": 100, "page": page, "sort": "date,asc", **window}
                if cc:
                    params["countryCode"] = cc
                data = _tm_get(s, "events.json", params, api_key)
                for ev in data.get("_embedded", {}).get("events", []):
                    e = parse_tm_event(ev, artist)
                    found.setdefault(e["key"], e)
                page += 1
                if page >= min(data.get("page", {}).get("totalPages", 1), 10):
                    break
    return list(found.values())
