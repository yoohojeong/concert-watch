"""웹페이지에서 추가한 아티스트(data/artists.json).

config.yaml의 artists에 더해 쓴다. 이름만 받아도 KOPIS·Ticketmaster·예매처 공지·뉴스 검색에 쓸 검색어를 만든다.
OPENAI_API_KEY가 있으면 한글/영문 이름과 다른 표기(예: 혁오 ↔ HYUKOH)를 찾아 검색어에 더한다.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
from pathlib import Path

import requests

log = logging.getLogger(__name__)
DEFAULT_EXCLUDE = ["트리뷰트", "tribute", "커버", "헌정"]
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
ALIAS_PROMPT = """You help match a music artist's name in Korean concert listings.
Given an artist name, return ONLY JSON: {"korean": "Korean name or null", "english": "English/romanized name or null",
"aliases": ["other common spellings used in ticket listings"], "is_artist": true/false}
Only include names you are confident refer to this artist. Keep aliases short (max 3). No prose."""


def load(path: Path) -> dict:
    if not path.exists():
        return {"artists": [], "removed": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log.error("data/artists.json을 읽지 못했어요")
        return {"artists": [], "removed": []}
    return {"artists": data.get("artists") or [], "removed": data.get("removed") or []}


def save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def merged(cfg: dict, data: dict) -> list[dict]:
    """config.yaml 아티스트 + 웹페이지에서 추가한 아티스트 - 웹페이지에서 뺀 아티스트."""
    removed = {n.lower() for n in data.get("removed") or []}
    out, names = [], set()
    for a in list(cfg.get("artists") or []) + list(data.get("artists") or []):
        key = a["name"].lower()
        if key in removed or key in names:
            continue
        names.add(key)
        out.append(a)
    return out


def _aliases(name: str) -> list[str]:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        return []
    try:
        r = requests.post(OPENAI_URL, timeout=30, headers={"Authorization": f"Bearer {key}"}, json={
            "model": "gpt-4o-mini", "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": ALIAS_PROMPT}, {"role": "user", "content": name}]})
        r.raise_for_status()
        data = json.loads(r.json()["choices"][0]["message"]["content"])
    except Exception as e:
        log.warning("'%s'의 다른 표기를 찾지 못했어요: %s", name, e)
        return []
    words = [data.get("korean"), data.get("english")] + list(data.get("aliases") or [])[:3]
    return [w.strip() for w in words if isinstance(w, str) and 2 <= len(w.strip()) <= 40]


def build(name: str, now: dt.datetime) -> dict:
    """이름 하나로 아티스트 설정을 만든다."""
    words = []
    for w in [name] + _aliases(name):
        if w.lower() not in {x.lower() for x in words}:
            words.append(w)
    english = next((w for w in words if re.fullmatch(r"[A-Za-z0-9 .&'!-]+", w)), name)
    return {
        "name": name, "kopis_keywords": words, "ticketmaster_keyword": english,
        "news_queries": [name], "exclude_keywords": DEFAULT_EXCLUDE,
        "added_from_page": now.isoformat(timespec="seconds"),
    }


def apply(data: dict, change: dict, cfg: dict, now: dt.datetime) -> tuple[str, str | None]:
    """아티스트 추가·빼기를 반영한다. (결과 문장, 새로 추가된 이름 또는 None)"""
    name = change["name"]
    low = name.lower()
    if change["action"] == "remove_artist":
        data["artists"] = [a for a in data["artists"] if a["name"].lower() != low]
        if low not in {n.lower() for n in data["removed"]}:
            data["removed"].append(name)
        return f"아티스트를 뺐어요: {name}", None
    data["removed"] = [n for n in data["removed"] if n.lower() != low]
    if any(a["name"].lower() == low for a in merged(cfg, data)):
        return f"이미 추적 중이에요: {name}", None
    data["artists"].append(build(name, now))
    return f"아티스트를 추가했어요: {name} (검색어 {', '.join(data['artists'][-1]['kopis_keywords'])})", name
