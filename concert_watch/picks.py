"""예매 계획(data/picks.json) 읽기·쓰기.

웹페이지의 '예매할게요'는 내용이 채워진 GitHub 이슈 작성 화면을 연다. 저장소 주인이 그 이슈를 제출하면
GitHub Actions가 이슈 본문의 JSON을 읽어 data/picks.json에 반영하고 이슈를 닫는다.
그래서 페이지에 GitHub 토큰을 넣을 필요가 없다(GitHub에 로그인만 돼 있으면 된다).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)
MARKER = "<!-- concert-watch-pick -->"
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


def parse_issue(body: str) -> dict:
    """이슈 본문에서 예매 계획 변경을 꺼내 검사한다. 형식이 틀리면 ValueError."""
    if MARKER not in (body or ""):
        raise ValueError("예매 계획 이슈가 아니에요")
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", body.split(MARKER, 1)[1], re.DOTALL)
    if not m:
        raise ValueError("이슈 본문에서 예매 계획을 찾지 못했어요")
    data = json.loads(m.group(1))
    action, key = data.get("action"), data.get("key")
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
        removed = picks.pop(key, None)
        return f"예매 계획을 취소했어요: {(removed or {}).get('artist') or key}"
    pick = dict(change["pick"])
    pick["picked_at"] = (picks.get(key) or {}).get("picked_at") or pick["picked_at"]
    picks[key] = pick
    when = "공식 발표에서 자동으로 찾기" if pick["mode"] == "auto" else f"직접 입력 {pick['open_at']}"
    return f"예매 계획을 저장했어요: {pick['artist']} {pick['title']} ({when})"
