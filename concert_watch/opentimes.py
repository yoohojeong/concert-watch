"""예매 오픈 일시 자동 찾기.

1. 공식 소식(인스타그램 글, 공식 사이트에 새로 생긴 줄)에서 예매 오픈 일시를 뽑는다.
   OPENAI_API_KEY가 있으면 OpenAI API로 읽고(형식이 제각각인 글, 영어 공지, '이번 주 금요일' 같은 표현까지),
   없으면 "11월 20일(목) 오후 8시", "2026.11.20 20:00" 같은 한국어·숫자 형식만 규칙으로 찾는다.
2. 공연마다 쓸 예매 오픈 일시(후보)를 고른다.
   - Ticketmaster 공연: Ticketmaster가 알려주는 선예매·일반 예매 시각
   - 공식 소식 자체: 그 글에서 찾은 시각
   - 그 밖의 공연: 같은 아티스트의 공식 소식에서 찾은 시각 중 나라가 맞고 공연 날짜보다 앞선 것
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger(__name__)
API_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = "gpt-4o-mini"
SOURCE_NAMES = {"instagram": "공식 인스타그램", "web": "공식 사이트", "news": "뉴스"}
EVENT_SOURCE_NAMES = {"ticketmaster": "Ticketmaster", "melon": "멜론티켓", "kopis": "KOPIS"}

SYSTEM_PROMPT = """You read concert announcements and extract when ticket sales open.
Return ONLY a JSON object, no prose, no code fences:
{"open_times": [{"label": "...", "date": "YYYY-MM-DD", "time": "HH:MM", "timezone": "IANA zone",
                 "country": "ISO 3166-1 alpha-2 or null", "city": "city or null", "evidence": "short phrase"}]}
Rules:
- Only moments when ticket sales or presales OPEN. Never output concert dates, door times, or sale end times.
- label in Korean: "팬클럽 선예매", "선예매", "일반 예매", or "예매" when the kind is unclear.
- Resolve relative dates ("this Friday", "tomorrow", "내일", "이번 주 금요일") using the post date given.
- timezone: if stated, use it (KST -> Asia/Seoul, BST -> Europe/London, ET -> America/New_York, ...).
  If the text is about Korean shows or Korean ticket sites (인터파크, 멜론티켓, YES24, 티켓링크), use Asia/Seoul.
  If it says "local time", use the time zone of the venue city; emit one entry per city when cities differ.
- country/city: where the concert for that sale takes place, if the text says so.
- Skip entries without a specific clock time. Return {"open_times": []} if there are none."""

KEYWORD_RE = re.compile(r"예매|티켓\s*오픈|선예매|팬클럽|on\s*sale|pre-?sale|ticket", re.IGNORECASE)
KR_HINT_RE = re.compile(r"서울|한국|내한|인터파크|멜론\s*티켓|예스24|yes24|티켓링크|KSPO|고척|잠실|올림픽", re.IGNORECASE)
DATETIME_RE = re.compile(
    r"(?:(20\d{2})\s*[년./-]\s*)?(\d{1,2})\s*(?:월|[./-])\s*(\d{1,2})\s*일?\s*"
    r"(?:\(\s*[월화수목금토일]\s*\))?\s*,?\s*(오전|오후|AM|PM)?\s*(\d{1,2})\s*(?:시|:)\s*(?:(\d{2})\s*분?)?\s*(AM|PM)?",
    re.IGNORECASE,
)


def _label_for(text: str) -> str:
    low = text.lower()
    if "팬클럽" in text or "fan club" in low or "fanclub" in low:
        return "팬클럽 선예매"
    if "선예매" in text or "presale" in low or "pre-sale" in low:
        return "선예매"
    if "일반" in text or "general" in low:
        return "일반 예매"
    return "예매"


def extract_by_rules(text: str, published: dt.datetime, tz: ZoneInfo) -> list[dict]:
    """한국어·숫자 날짜 형식에서 예매 오픈 일시를 찾는다(API 키가 없을 때)."""
    country = "KR" if KR_HINT_RE.search(text or "") else None
    lines = [l.strip() for l in re.split(r"\n|\s/\s", text or "") if l.strip()]
    found = []
    for i, line in enumerate(lines):
        context = (lines[i - 1] + " " if i else "") + line
        if not KEYWORD_RE.search(context):
            continue
        for m in DATETIME_RE.finditer(line):
            year, month, day, ampm1, hour, minute, ampm2 = m.groups()
            hour, minute = int(hour), int(minute or 0)
            ampm = (ampm1 or ampm2 or "").lower()
            if ampm in ("오후", "pm") and hour < 12:
                hour += 12
            elif ampm in ("오전", "am") and hour == 12:
                hour = 0
            try:
                y = int(year) if year else published.astimezone(tz).year
                local = dt.datetime(y, int(month), int(day), hour, minute, tzinfo=tz)
                if not year and local < published - dt.timedelta(days=2):
                    local = local.replace(year=y + 1)   # 연도가 없으면 다가오는 날짜로
            except ValueError:
                continue
            label = _label_for(line)
            if label == "예매":
                label = _label_for(context)
            found.append({"label": label, "at": local.astimezone(dt.timezone.utc).isoformat(),
                          "country": country, "city": None, "evidence": line[:80], "method": "rules"})
    return _dedupe(found)


def _parse_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.MULTILINE).strip()
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start:end + 1]) if start >= 0 else {}


def extract_with_openai(text: str, artist: str, published: dt.datetime, default_tz: ZoneInfo,
                        api_key: str, model: str) -> list[dict]:
    prompt = (f"Artist: {artist}\nPost date: {published.isoformat(timespec='minutes')}\n\n"
              f"Text:\n{(text or '')[:6000]}")
    r = requests.post(API_URL, timeout=60, headers={
        "Authorization": f"Bearer {api_key}", "content-type": "application/json",
    }, json={"model": model, "response_format": {"type": "json_object"},
             "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                          {"role": "user", "content": prompt}]})
    if r.status_code >= 400:
        raise RuntimeError(f"OpenAI API {r.status_code}: {r.text[:200]}")
    choices = r.json().get("choices") or [{}]
    reply = (choices[0].get("message") or {}).get("content") or ""
    found = []
    for item in (_parse_json(reply).get("open_times") or []):
        try:
            tz = ZoneInfo(item.get("timezone") or "")
        except Exception:
            tz = default_tz
        try:
            local = dt.datetime.fromisoformat(f"{item['date']}T{item['time']}").replace(tzinfo=tz)
        except (KeyError, TypeError, ValueError):
            continue
        country = (item.get("country") or "").upper() or None
        found.append({"label": item.get("label") or "예매", "at": local.astimezone(dt.timezone.utc).isoformat(),
                      "country": country if country and len(country) == 2 else None,
                      "city": item.get("city"), "evidence": (item.get("evidence") or "")[:80], "method": "openai"})
    return _dedupe(found)


def extract(text: str, artist: str, published: dt.datetime, cfg: dict) -> list[dict]:
    """공식 소식 원문에서 예매 오픈 일시 목록을 찾는다. 실패하면 규칙 방식으로 대신한다."""
    tz = ZoneInfo(cfg.get("timezone", "Asia/Seoul"))
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        model = (cfg.get("open_time") or {}).get("model") or DEFAULT_MODEL
        try:
            return extract_with_openai(text, artist, published, tz, key, model)
        except Exception as e:
            log.warning("OpenAI로 예매 오픈 일시를 찾지 못해 규칙 방식으로 대신해요: %s", e)
    return extract_by_rules(text, published, tz)


def _dedupe(items: list[dict]) -> list[dict]:
    seen, out = set(), []
    for c in sorted(items, key=lambda c: c["at"]):
        k = (c["label"], c["at"][:16])
        if k not in seen:
            seen.add(k)
            out.append(c)
    return out


# ── 공연마다 쓸 예매 오픈 일시 고르기 ─────────────────────────
def candidates_for(key: str, info: dict, state: dict) -> list[dict]:
    """key(공연·소식)에 맞는 예매 오픈 일시 후보. info에는 artist, country, start가 들어간다."""
    events = state.get("events") or {}
    news = state.get("announcements") or {}
    ev = events.get(key)
    out: list[dict] = []

    if ev:
        out += [{**c, "source": EVENT_SOURCE_NAMES.get(ev.get("source"), "예매처"), "source_url": ev.get("url", ""),
                 "found_at": ev.get("first_seen", "")} for c in ev.get("sale_times") or []]

    own = news.get(key)
    if own:
        out += [{**c, "source": SOURCE_NAMES.get(own.get("source"), "공식 채널"),
                 "source_url": own.get("url", ""), "found_at": own.get("first_seen", "")}
                for c in own.get("open_times") or []]
    else:
        artist = info.get("artist") or (ev or {}).get("artist")
        country = ((ev or {}).get("country") or info.get("country") or "").upper()
        start = (ev or {}).get("start") or info.get("start") or ""
        for n in news.values():
            if n.get("artist") != artist:
                continue
            for c in n.get("open_times") or []:
                if country and c.get("country") and c["country"].upper() != country:
                    continue                          # 다른 나라 공연의 예매
                if start and c["at"][:10] > start:
                    continue                          # 공연보다 늦은 예매 오픈은 다른 공연
                out.append({**c, "source": SOURCE_NAMES.get(n.get("source"), "공식 채널"),
                            "source_url": n.get("url", ""), "found_at": n.get("first_seen", "")})
    return _dedupe(out)


def effective_times(key: str, pick: dict, state: dict) -> list[dict]:
    """리마인더에 쓸 예매 오픈 일시. 직접 넣은 시간이 있으면 그것만, 자동이면 찾은 후보 전부."""
    manual = pick.get("mode") == "manual" or (pick.get("open_at") and pick.get("mode") != "auto")
    if manual:
        return [{"label": "예매", "at": pick["open_at"], "source": "직접 입력"}] if pick.get("open_at") else []
    return candidates_for(key, pick, state)
