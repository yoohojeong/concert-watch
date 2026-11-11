"""예매 계획(data/picks.json) 읽기·쓰기.

웹페이지에서 바꾼 예매 계획은 ntfy를 거쳐 들어오고(concert_watch/remote.py), 여기서 검사해 data/picks.json에 반영한다.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
from pathlib import Path

from .notify import anchor_id

log = logging.getLogger(__name__)
TEXT_FIELDS = ("artist", "title", "anchor", "country", "start", "link", "picked_at", "open_at", "mode")
MAX_LEN = 500


def load(path: Path) -> dict:
    """웹페이지에서 '예매할게요'로 고른 공연."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("picks") or {}
    except (json.JSONDecodeError, AttributeError):
        log.error("data/picks.json을 읽지 못했어요")
        return {}


def save(path: Path, picks: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"picks": picks}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def validate_change(data: dict) -> dict:
    """웹페이지가 보낸 예매 계획 변경을 검사한다. 형식이 틀리면 ValueError."""
    if not isinstance(data, dict):
        raise ValueError("변경 내용이 올바르지 않아요")
    action, key = data.get("action"), data.get("key")
    if action in ("add_artist", "remove_artist"):
        name = " ".join(str(data.get("name") or "").split())
        if not 1 <= len(name) <= 40 or re.search(r"[<>{}\\]", name):
            raise ValueError("아티스트 이름이 올바르지 않아요")
        return {"action": action, "name": name}
    if action not in ("set", "delete"):
        raise ValueError(f"알 수 없는 동작: {action!r}")
    if not isinstance(key, str) or not key or len(key) > 200:
        raise ValueError("공연 키가 올바르지 않아요")
    if action == "delete":
        return {"action": "delete", "key": key}

    raw = data.get("pick")
    if not isinstance(raw, dict):
        raise ValueError("예매 계획 내용이 없어요")
    pick = {f: str(raw.get(f) or "")[:MAX_LEN] for f in TEXT_FIELDS}
    if pick["mode"] not in ("auto", "manual"):
        raise ValueError("mode는 auto 또는 manual이어야 해요")
    if pick["mode"] == "manual":
        try:
            dt.datetime.fromisoformat(pick["open_at"].replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("직접 입력한 예매 오픈 일시가 올바르지 않아요") from None
    else:
        pick["open_at"] = ""
    if pick["link"] and not pick["link"].startswith(("https://", "http://")):
        pick["link"] = ""
    return {"action": "set", "key": key, "pick": pick}


def apply(picks: dict, change: dict) -> str:
    """변경을 picks에 반영하고 결과를 한 줄로 돌려준다."""
    key = change["key"]
    if change["action"] == "delete":
        # 지우지 않고 꺼둔다. 그래야 자동 리마인더가 같은 공연을 다시 넣지 않는다.
        rec = picks.get(key) or {}
        picks[key] = {**rec, "mode": "off"}
        return f"예매 계획을 취소했어요: {rec.get('artist') or key}"
    pick = dict(change["pick"])
    pick["picked_at"] = (picks.get(key) or {}).get("picked_at") or pick["picked_at"]
    picks[key] = pick
    when = "공식 발표에서 자동으로 찾기" if pick["mode"] == "auto" else f"직접 입력 {pick['open_at']}"
    return f"예매 계획을 저장했어요: {pick['artist']} {pick['title']} ({when})"


def active(picks: dict) -> dict:
    """취소(off)하지 않은 예매 계획."""
    return {k: p for k, p in picks.items() if (p or {}).get("mode") != "off"}


def auto_add(picks: dict, state: dict, cfg: dict, now: dt.datetime) -> list[str]:
    """예매 오픈 시각이 나온 공연을 예매 계획에 자동으로 넣는다(config reminders.auto).

    이미 있는 계획(취소한 것 포함)은 건드리지 않는다. 추가한 공연 키 목록을 돌려준다.
    """
    rcfg = cfg.get("reminders") or {}
    if not rcfg.get("auto", True):
        return []
    countries = {c.upper() for c in rcfg.get("auto_countries", ["KR"]) or []}
    tracked = {a["name"] for a in cfg.get("artists") or []}
    added = []
    for key, e in (state.get("events") or {}).items():
        if key in picks or e.get("artist") not in tracked:
            continue
        if countries and (e.get("country") or "").upper() not in countries:
            continue
        future = []
        for c in e.get("sale_times") or []:
            try:
                if dt.datetime.fromisoformat(c["at"].replace("Z", "+00:00")) > now:
                    future.append(c)
            except (KeyError, ValueError):
                pass
        if not future:
            continue
        links = [l.get("url", "") for l in e.get("ticket_links") or [] if l.get("url", "").startswith("http")]
        picks[key] = {
            "artist": e.get("artist", ""), "title": e.get("title", ""), "anchor": e.get("anchor") or anchor_id(key),
            "country": e.get("country", ""), "start": e.get("start", ""), "mode": "auto", "open_at": "",
            "link": (links or [e.get("url", "")])[0], "picked_at": now.isoformat(timespec="seconds"), "auto": True,
        }
        added.append(key)
    return added
