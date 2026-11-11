"""공연 정보 소스.

- KOPIS(공연예술통합전산망) 오픈API: 국내 공연. 정부가 운영하는 공식 공연 DB.
- Ticketmaster Discovery API: 해외 공연(북미·유럽·호주 등).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
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


# ── 멜론티켓 티켓오픈소식 ─────────────────────────────────
# 공식 API가 없어 '티켓오픈소식' 공지 페이지를 읽는다. 공지 번호(csoonId)가 차례로 늘어나므로
# 지난번에 본 번호 다음부터 새 공지를 확인한다. KOPIS보다 먼저, 예매 오픈 시각까지 나온다.
MELON_BASE = "https://ticket.melon.com"
MELON_LIST = MELON_BASE + "/csoon/ajax/listTicketOpen.htm?orderType=0&pageIndex=1&schGcode=GENRE_CON_ALL"
MELON_DETAIL = MELON_BASE + "/csoon/detail.htm?csoonId={id}"
MELON_PRODUCT = MELON_BASE + "/performance/index.htm?prodId={id}"
MELON_BACKFILL = 150      # 처음 돌릴 때 거슬러 올라가 볼 공지 수(약 2~3주)
MELON_MAX_PER_RUN = 200     # 처음 거슬러 보는 공지(MELON_BACKFILL)를 한 번에 다 볼 수 있게
MELON_PAUSE = 0.3
MELON_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36", "Accept-Language": "ko-KR,ko;q=0.9"}
_KST = dt.timezone(dt.timedelta(hours=9))
_KO_DATETIME = re.compile(r"(20\d{2})년\s*(\d{1,2})월\s*(\d{1,2})일[^0-9]*?(\d{1,2}):(\d{2})")
_KO_DATE = re.compile(r"(20\d{2})년\s*(\d{1,2})월\s*(\d{1,2})일")


def _strip_tags(s: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", s or "").replace("&amp;", "&").replace("&lt;", "<")
                    .replace("&gt;", ">").replace("&nbsp;", " ").split())


def _site_get(session: requests.Session, url: str, tries: int = 3) -> requests.Response:
    """예매처 페이지 요청. 일시 차단(423·429)이나 서버 오류면 점점 길게 쉬었다 다시 시도한다."""
    for attempt in range(tries):
        r = session.get(url, headers=MELON_HEADERS, timeout=TIMEOUT)
        if r.status_code not in (423, 429) and r.status_code < 500:
            return r
        if attempt < tries - 1:
            time.sleep(5 * (3 ** attempt))      # 5초, 15초
    return r


def melon_latest_id(session: requests.Session) -> int:
    r = _site_get(session, MELON_LIST)
    r.raise_for_status()
    ids = [int(i) for i in re.findall(r"csoonId=(\d+)", r.text)]
    if not ids:
        raise SourceError("멜론티켓 티켓오픈소식 목록을 읽지 못했어요")
    return max(ids)


def parse_melon_notice(html: str, csoon_id: int) -> dict | None:
    """멜론티켓 티켓오픈 공지 한 건. 공지가 없거나 형식이 다르면 None."""
    m = re.search(r'<p class="tit">(.*?)</p>', html, re.S)
    if not m:
        return None
    title = re.sub(r"\s*티켓\s*오픈\s*안내\s*$", "", _strip_tags(m.group(1)))
    sale_times = []
    for label, when in re.findall(r'class="tit_type">(.*?)</dt>\s*<dd class="txt_date">(.*?)</dd>', html, re.S):
        d = _KO_DATETIME.search(_strip_tags(when))
        if not d:
            continue
        y, mo, da, h, mi = map(int, d.groups())
        at = dt.datetime(y, mo, da, h, mi, tzinfo=_KST).astimezone(dt.timezone.utc).isoformat()
        label = _strip_tags(label)
        label = "일반 예매" if label.replace(" ", "") in ("티켓오픈", "일반예매") else label
        sale_times.append({"label": label, "at": at})
    singers = [_strip_tags(s) for s in re.findall(r'class="singer">(.*?)</strong>', html, re.S)]
    text = _strip_tags(re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S))
    start = end = ""
    pm = re.search(r"공연\s*(?:일|기간|일시)\s*:\s*([^-]{0,80})", text)
    if pm and (d := _KO_DATE.search(pm.group(1))):
        first = dt.date(*map(int, d.groups()))
        start = end = first.isoformat()
        # "2027년 1월 16일(토) 18시 / 17일(일) 17시"처럼 이어지는 날짜는 같은 달로 본다
        days = [int(x) for x in re.findall(r"(\d{1,2})일", pm.group(1)[d.end() - 3:])]
        if days and max(days) > first.day:
            try:
                end = first.replace(day=max(days)).isoformat()
            except ValueError:
                pass
    vm = re.search(r"공연\s*장소\s*:\s*(.+?)(?:\s+-\s|\s+티켓\s*가격|\s+기획사|$)", text)
    prod = re.search(r"bannerLanding\('TD',\s*'(\d+)'\)", html)
    url = MELON_DETAIL.format(id=csoon_id)
    return {
        "key": f"melon:{csoon_id}", "source": "melon", "title": title,
        "start": start, "end": end, "time": "", "venue": (vm.group(1).strip()[:80] if vm else ""),
        "city": "", "country": "KR", "url": url,
        "ticket_links": [{"name": "멜론티켓", "url": MELON_PRODUCT.format(id=prod.group(1)) if prod else url}],
        "sale_start": next((c["at"] for c in sale_times if c["label"] == "일반 예매"), ""),
        "sale_times": sorted(sale_times, key=lambda c: c["at"]),
        "_match_text": " ".join([title] + singers),
    }


def fetch_melon(artists: list[dict], state: dict, session: requests.Session | None = None) -> list[dict]:
    """지난번 이후 새로 올라온 멜론티켓 티켓오픈 공지 중 추적 중인 아티스트 공연."""
    s = session or requests.Session()
    latest = melon_latest_id(s)
    last = int(state.get("melon_last_id") or (latest - MELON_BACKFILL))
    found = []
    stop = min(latest, last + MELON_MAX_PER_RUN)
    for csoon_id in range(last + 1, stop + 1):
        time.sleep(MELON_PAUSE)
        r = _site_get(s, MELON_DETAIL.format(id=csoon_id))
        if r.status_code == 404:
            continue
        if not r.ok:
            # 여기까지 본 것은 저장하고, 나머지는 다음 실행에서 이어서 본다
            state["melon_last_id"] = csoon_id - 1
            log.warning("멜론티켓 공지 %s에서 멈췄어요(HTTP %s). 다음에 이어서 볼게요", csoon_id, r.status_code)
            return found
        notice = parse_melon_notice(r.text, csoon_id)
        if not notice:
            continue
        artist = _match_artist(notice.pop("_match_text"), artists)
        if artist:
            notice["artist"] = artist["name"]
            found.append(notice)
            log.info("멜론티켓 공지 %s: %s %s", csoon_id, artist["name"], notice["title"])
    state["melon_last_id"] = stop
    return found


def _match_artist(text: str, artists: list[dict]) -> dict | None:
    """공지 제목·출연진 글에 추적 중인 아티스트 이름(또는 키워드)이 있으면 그 아티스트."""
    hay = (text or "").lower()
    for a in artists:
        words = [a["name"], a.get("ticketmaster_keyword") or ""] + list(a.get("kopis_keywords") or [])
        if any(w and w.lower() in hay for w in words) and \
                not any(x and x.lower() in hay for x in a.get("exclude_keywords") or []):
            return a
    return None


def _kst_iso(y: int, mo: int, d: int, h: int, mi: int) -> str:
    return dt.datetime(y, mo, d, h, mi, tzinfo=_KST).astimezone(dt.timezone.utc).isoformat()


def _sale_label(name: str) -> str:
    n = (name or "").replace(" ", "")
    return "일반 예매" if n in ("", "티켓오픈", "일반예매", "일반예매일", "일반오픈") else " ".join(name.split())


# ── 인터파크(NOL) 티켓오픈 공지 ───────────────────────────
# 공지 목록 페이지 안에 공지 데이터(JSON)가 들어 있어 한 번 요청으로 다가오는 오픈 공지를 모두 읽는다.
INTERPARK_LIST = "https://tickets.interpark.com/contents/notice"
INTERPARK_GOODS = "https://tickets.interpark.com/goods/{code}"


def parse_interpark_notices(html: str) -> list[dict]:
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', html)
    text = "".join(json.loads(f'"{c}"') for c in chunks)
    dec, items = json.JSONDecoder(), {}
    for m in re.finditer(r'\{"ticket_dates":', text):
        try:
            obj, _ = dec.raw_decode(text, m.start())
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("id"):
            items[obj["id"]] = obj
    return list(items.values())


def interpark_event(n: dict) -> dict:
    sale_times = []
    for d in n.get("ticket_dates") or []:
        try:
            local = dt.datetime.fromisoformat(d["ticket_open_date"]).replace(tzinfo=_KST)
        except (KeyError, TypeError, ValueError):
            continue
        label = d.get("ticket_other_open_name") or d.get("ticket_open_type_name") or ""
        sale_times.append({"label": _sale_label(label), "at": local.astimezone(dt.timezone.utc).isoformat()})
    sale_times.sort(key=lambda c: c["at"])
    start = (n.get("goods_start_date") or "")[:10]
    code = n.get("goods_code") or ""
    url = INTERPARK_GOODS.format(code=code) if code else INTERPARK_LIST
    return {
        "key": f"interpark:{n['id']}", "source": "interpark", "title": n.get("title") or n.get("goods_name") or "",
        "start": start, "end": (n.get("goods_end_date") or "")[:10] or start, "time": "",
        "venue": n.get("venue_name") or "", "city": "", "country": "KR", "url": url,
        "ticket_links": [{"name": "인터파크", "url": url}],
        "sale_start": next((c["at"] for c in sale_times if c["label"] == "일반 예매"), ""),
        "sale_times": sale_times,
    }


def fetch_interpark(artists: list[dict], session: requests.Session | None = None) -> list[dict]:
    s = session or requests.Session()
    r = _site_get(s, INTERPARK_LIST)
    r.raise_for_status()
    notices = parse_interpark_notices(r.text)
    if not notices:
        raise SourceError("인터파크 티켓오픈 공지를 읽지 못했어요(페이지 구조가 바뀌었을 수 있어요)")
    found = []
    for n in notices:
        artist = _match_artist(" ".join([n.get("title") or "", n.get("goods_name") or "",
                                         re.sub(r"<[^>]+>", " ", n.get("casting_info") or "")]), artists)
        if artist:
            e = interpark_event(n)
            e["artist"] = artist["name"]
            found.append(e)
            log.info("인터파크 공지 %s: %s %s", n["id"], artist["name"], e["title"])
    return found


# ── 예스24 티켓오픈 공지 ─────────────────────────────────
YES24_LIST = "http://ticket.yes24.com/New/Notice/Ajax/axList.aspx"
YES24_READ = "http://ticket.yes24.com/New/Notice/Ajax/axRead.aspx"
YES24_NOTICE = "http://ticket.yes24.com/Notice?#id={id}"
YES24_PERF = "http://ticket.yes24.com/Perf/{id}"
YES24_PAGES = 2
YES24_PAGE_SIZE = 20
_YES24_DT = re.compile(r"(20\d{2})\.(\d{1,2})\.(\d{1,2})[^0-9]*?(오전|오후)?\s*(\d{1,2}):(\d{2})")


def parse_yes24_notice(html: str, notice_id: int) -> dict | None:
    m = re.search(r"id='hdNotiTitle' value='(.*?)'", html, re.S)
    if not m:
        return None
    title = re.sub(r"\s*티켓\s*오픈\s*안내\s*$", "", _strip_tags(m.group(1)))
    sale_times = []
    box = (re.search(r'class="noti-vt-box">(.*?)class="noti-vt-btns"', html, re.S) or re.search(r"(.*)", html, re.S)).group(1)
    for label, when in re.findall(r'<span>\s*(?:<em>)?([^<]*?)(?:</em>)?\s*</span>\s*<span id="title\d+">([^<]*)</span>', box):
        d = _YES24_DT.search(_strip_tags(when))
        if not d:
            continue
        y, mo, da, ampm, h, mi = d.groups()
        h = int(h)
        if ampm == "오후" and h < 12:
            h += 12
        elif ampm == "오전" and h == 12:
            h = 0
        sale_times.append({"label": _sale_label(_strip_tags(label)), "at": _kst_iso(int(y), int(mo), int(da), h, int(mi))})
    om = re.search(r"noti-view-comen-txt'>(.*?)</div>", html, re.S)
    overview = _strip_tags(re.sub(r"<br\s*/?>", " ", om.group(1))) if om else ""
    start = ""
    pm = re.search(r"공연\s*(?:일자|일|기간|일시)\s*:\s*(.{0,60})", overview)
    if pm and (d := _KO_DATE.search(pm.group(1))):
        start = dt.date(*map(int, d.groups())).isoformat()
    vm = re.search(r"공연\s*장소\s*:\s*(.+?)(?:\s+-\s|$)", overview)
    perf = re.search(r"GetSiteDetailURL\((\d+)\)", html)
    url = YES24_PERF.format(id=perf.group(1)) if perf else YES24_NOTICE.format(id=notice_id)
    return {
        "key": f"yes24:{notice_id}", "source": "yes24", "title": title, "start": start, "end": start, "time": "",
        "venue": vm.group(1).strip()[:80] if vm else "", "city": "", "country": "KR", "url": url,
        "ticket_links": [{"name": "예스24", "url": url}],
        "sale_start": next((c["at"] for c in sale_times if c["label"] == "일반 예매"), ""),
        "sale_times": sorted(sale_times, key=lambda c: c["at"]),
        "_match_text": " ".join([title, overview[:300]]),
    }


def fetch_yes24(artists: list[dict], state: dict, session: requests.Session | None = None) -> list[dict]:
    """예스24 티켓오픈 공지 목록에서 처음 보는 공지만 열어 추적 중인 아티스트 공연을 찾는다."""
    s = session or requests.Session()
    ids: list[int] = []
    for page in range(1, YES24_PAGES + 1):
        r = s.post(YES24_LIST, data={"page": page, "size": YES24_PAGE_SIZE, "genre": "", "province": "",
                                     "order": 1, "searchType": "All", "searchText": ""},
                   headers=MELON_HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        ids += [int(i) for i in re.findall(r'href="#id=(\d+)"', r.text)]
    if not ids:
        raise SourceError("예스24 티켓오픈 공지 목록을 읽지 못했어요")
    seen = state.setdefault("yes24_seen", [])
    seen_set = set(seen)
    found = []
    for nid in sorted(set(ids) - seen_set):
        time.sleep(MELON_PAUSE)
        r = s.post(YES24_READ, data={"bId": nid, "genre": "", "province": "", "order": 1},
                   headers=MELON_HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        notice = parse_yes24_notice(r.text, nid)
        seen.append(nid)
        if not notice:
            continue
        artist = _match_artist(notice.pop("_match_text"), artists)
        if artist:
            notice["artist"] = artist["name"]
            found.append(notice)
            log.info("예스24 공지 %s: %s %s", nid, artist["name"], notice["title"])
    del seen[:-1000]
    return found
