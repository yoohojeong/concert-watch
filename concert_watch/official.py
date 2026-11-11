"""공식 채널 감시.

- 인스타그램: Meta의 공식 Instagram Graph API(Business Discovery)로 공개 게시물을 읽는다.
  인스타그램 화면을 긁어오는 방식은 로그인 요구와 차단 때문에 자동 실행에서 금방 막히고 약관에도 어긋나서 쓰지 않는다.
- 공식 웹사이트: 페이지의 글자를 읽어 처음 보는 줄 중 날짜나 공연 관련 단어가 들어간 줄이 생기면 알린다.

두 경우 모두 처음 확인할 때는 지금 있는 내용을 기준으로 저장만 하고, 그 뒤로 새로 생긴 것만 알린다.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import os
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import quote_plus

import requests

log = logging.getLogger(__name__)
TIMEOUT = 25
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}
GRAPH_BASE = "https://graph.facebook.com"
SEEN_LIMIT = 5000

DEFAULT_KEYWORDS = [
    "tour", "concert", "tickets", "ticket", "on sale", "presale", "pre-sale", "dates", "festival",
    "공연", "콘서트", "투어", "내한", "티켓", "예매", "단독", "페스티벌", "선예매",
]
_MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
DATE_RE = re.compile(
    rf"(\b20\d{{2}}\s?[./-]\s?\d{{1,2}}"            # 2026.12 / 2026-12
    rf"|\b(1[0-2]|0?[1-9])\s?[./]\s?(3[01]|[12]\d|0?[1-9])\b"  # 12.24 / 12/24 (가격 29.99는 제외)
    rf"|\d{{1,2}}\s?월\s?\d{{1,2}}\s?일"              # 12월 24일
    rf"|\b({_MONTHS})[a-z]*\.?\s+\d{{1,2}}\b"         # Dec 24
    rf"|\b\d{{1,2}}\s+({_MONTHS})[a-z]*\b)",          # 24 Dec
    re.IGNORECASE,
)


class SourceError(Exception):
    pass


def _h(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def mentions_show(text: str, keywords: list[str]) -> bool:
    low = (text or "").lower()
    return any(k.lower() in low for k in keywords)


# ── 웹페이지 ───────────────────────────────────────────
class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template", "head", "iframe"}
    BLOCK = {"p", "div", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6", "section",
             "article", "header", "footer", "br", "dd", "dt", "ul", "ol", "table"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip_depth += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip_depth = max(0, self.skip_depth - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip_depth:
            self.parts.append(data)


def page_lines(html_text: str) -> list[str]:
    parser = _TextExtractor()
    parser.feed(html_text)
    out, seen = [], set()
    for raw in "".join(parser.parts).split("\n"):
        line = " ".join(raw.split())
        if 3 <= len(line) <= 300 and line not in seen:
            seen.add(line)
            out.append(line)
    return out


def fetch_page_lines(url: str) -> list[str]:
    r = requests.get(url, headers=BROWSER_HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    lines = page_lines(r.text)
    if len(lines) < 5:
        raise SourceError("페이지에서 읽을 글자가 거의 없어요(자바스크립트로 그리는 페이지일 수 있어요)")
    return lines


# ── 뉴스 검색(Google News RSS) ─────────────────────────
# 공식 사이트가 없거나 읽을 수 없는 아티스트용. 공연 발표 기사만 골라서 알린다.
NEWS_URL = "https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"
NEWS_MAX_AGE_DAYS = 3        # 이보다 오래된 기사는 새로 보여도 무시
NEWS_ANNOUNCE_RE = re.compile(
    r"예매|티켓|오픈|개최|단독\s*(공연|콘서트)|투어|내한|라인업|출연|"
    r"tour|tickets?|on\s*sale|presale|announce", re.IGNORECASE)
NEWS_SKIP_RE = re.compile(r"성료|마쳐|마친|마무리|후기|리뷰|현장|열광|매진\s*행렬", re.IGNORECASE)


def fetch_news(query: str) -> list[dict]:
    r = requests.get(NEWS_URL.format(q=quote_plus(query)), headers=BROWSER_HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    items = []
    for it in ET.fromstring(r.content).iter("item"):
        title, link = (it.findtext("title") or "").strip(), (it.findtext("link") or "").strip()
        try:
            published = parsedate_to_datetime(it.findtext("pubDate") or "")
        except (TypeError, ValueError):
            published = None
        if title and link:
            items.append({"id": _h(link), "title": title, "link": link, "published": published})
    return items


def is_announcement(title: str, artist_words: list[str]) -> bool:
    """아티스트 이름이 들어 있고, 공연 발표 같고, 지난 공연 후기는 아닌 기사 제목."""
    low = title.lower()
    return (any(w.lower() in low for w in artist_words) and bool(NEWS_ANNOUNCE_RE.search(title))
            and not NEWS_SKIP_RE.search(title))


# ── 인스타그램 ─────────────────────────────────────────
def fetch_instagram(username: str, ig_user_id: str, token: str, version: str, limit: int = 12) -> list[dict]:
    fields = f"business_discovery.username({username}){{media.limit({limit}){{id,caption,permalink,timestamp}}}}"
    r = requests.get(f"{GRAPH_BASE}/{version}/{ig_user_id}",
                     params={"fields": fields, "access_token": token}, timeout=TIMEOUT)
    data = r.json() if r.content else {}
    if r.status_code >= 400 or "error" in data:
        msg = (data.get("error") or {}).get("message") or f"HTTP {r.status_code}"
        raise SourceError(msg[:200])
    return (data.get("business_discovery") or {}).get("media", {}).get("data", [])


def _first_line(text: str, width: int) -> str:
    line = next((l.strip() for l in (text or "").splitlines() if l.strip()), "")
    return line if len(line) <= width else line[: width - 1] + "…"


def _clip(text: str, width: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


# ── 실행 ──────────────────────────────────────────────
def check(cfg: dict, state: dict, now: dt.datetime) -> tuple[list[dict], list[str]]:
    """공식 채널을 확인하고 새 소식 목록과 오류 목록을 돌려준다."""
    keywords = (cfg.get("official") or {}).get("keywords") or DEFAULT_KEYWORDS
    ig_state = state.setdefault("instagram_seen", {})
    page_state = state.setdefault("page_seen", {})
    news: list[dict] = []
    errors: list[str] = []
    now_iso = now.isoformat(timespec="seconds")

    ig_user, ig_token = os.environ.get("IG_USER_ID"), os.environ.get("IG_ACCESS_TOKEN")
    ig_version = os.environ.get("IG_GRAPH_VERSION") or "v23.0"
    wants_ig = any(a.get("instagram") for a in cfg.get("artists") or [])
    if wants_ig and not (ig_user and ig_token):
        log.info("IG_USER_ID / IG_ACCESS_TOKEN이 없어 인스타그램 확인을 건너뛰어요")

    for artist in cfg.get("artists") or []:
        name = artist["name"]

        handle = artist.get("instagram")
        if handle and ig_user and ig_token:
            try:
                posts = fetch_instagram(handle, ig_user, ig_token, ig_version)
                seen = ig_state.get(handle)
                ids = [p["id"] for p in posts if p.get("id")]
                if seen is None:
                    ig_state[handle] = ids
                    log.info("@%s 게시물 %d개를 기준으로 저장했어요", handle, len(ids))
                else:
                    seen_set = set(seen)
                    for p in sorted(posts, key=lambda p: p.get("timestamp", "")):
                        if p.get("id") in seen_set:
                            continue
                        caption = p.get("caption", "")
                        if mentions_show(caption, keywords):
                            news.append({
                                "key": f"ig:{p['id']}", "kind": "announcement", "source": "instagram",
                                "artist": name, "title": _first_line(caption, 60) or f"@{handle} 새 게시물",
                                "summary": _clip(caption, 220), "text": (caption or "")[:4000],
                                "url": p.get("permalink", ""),
                                "published": p.get("timestamp", now_iso), "first_seen": now_iso,
                            })
                    ig_state[handle] = (ids + [i for i in seen if i not in ids])[:200]
            except Exception as e:
                errors.append(f"인스타그램 @{handle}({e.__class__.__name__})")
                log.error("인스타그램 @%s 확인 실패: %s", handle, e)

        for url in artist.get("official_pages") or []:
            try:
                lines = fetch_page_lines(url)
            except Exception as e:
                errors.append(f"공식 사이트 {url}({e.__class__.__name__})")
                log.error("공식 사이트 %s 확인 실패: %s", url, e)
                continue
            hashes = [_h(l) for l in lines]
            seen = page_state.get(url)
            if seen is None:
                page_state[url] = hashes[:SEEN_LIMIT]
                log.info("%s 내용 %d줄을 기준으로 저장했어요", url, len(lines))
                continue
            seen_set = set(seen)
            added = [l for l, h in zip(lines, hashes)
                     if h not in seen_set and (DATE_RE.search(l) or mentions_show(l, keywords))]
            page_state[url] = (hashes + [h for h in seen if h not in set(hashes)])[:SEEN_LIMIT]
            if added:
                news.append({
                    "key": f"web:{_h(url + '|'.join(added))}", "kind": "announcement", "source": "web",
                    "artist": name, "title": "공식 사이트에 새 내용이 올라왔어요",
                    "summary": _clip(" / ".join(added[:5]), 220), "text": "\n".join(added)[:4000], "url": url,
                    "published": now_iso, "first_seen": now_iso,
                })
        for query in artist.get("news_queries") or []:
            try:
                items = fetch_news(query)
            except Exception as e:
                errors.append(f"뉴스 검색 '{query}'({e.__class__.__name__})")
                log.error("뉴스 검색 '%s' 확인 실패: %s", query, e)
                continue
            skey = f"news:{query}"
            seen = page_state.get(skey)
            ids = [i["id"] for i in items]
            if seen is None:
                page_state[skey] = ids[:SEEN_LIMIT]
                log.info("뉴스 검색 '%s' 기사 %d개를 기준으로 저장했어요", query, len(ids))
                continue
            seen_set = set(seen)
            words = [query] + [w for w in (artist.get("kopis_keywords") or []) if w]
            cutoff = now - dt.timedelta(days=NEWS_MAX_AGE_DAYS)
            fresh = [i for i in items if i["id"] not in seen_set and is_announcement(i["title"], words)
                     and (i["published"] is None or i["published"] >= cutoff)]
            page_state[skey] = (ids + [h for h in seen if h not in set(ids)])[:SEEN_LIMIT]
            if fresh:
                fresh.sort(key=lambda i: i["published"] or now, reverse=True)
                top = fresh[0]
                news.append({
                    "key": f"news:{_h('|'.join(i['id'] for i in fresh))}", "kind": "announcement", "source": "news",
                    "artist": name, "title": _clip(top["title"], 80),
                    "summary": _clip(" / ".join(i["title"] for i in fresh[:5]), 220),
                    "text": "\n".join(i["title"] for i in fresh)[:4000], "url": top["link"],
                    "published": (top["published"] or now).isoformat(), "first_seen": now_iso,
                })
    return news, errors
