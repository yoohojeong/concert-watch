"""GitHub 로그인 없이 웹페이지에서 예매 계획을 바꾸기.

웹페이지는 변경 내용을 비밀문구(Secret PICK_PASSPHRASE)로 서명해서 ntfy 토픽(config site.edit_topic)에 올린다.
Actions가 매 실행마다 그 토픽의 메시지를 읽어 서명을 확인하고 data/picks.json에 반영한다.
토픽 이름은 공개돼 있어도 비밀문구를 모르면 서명을 만들 수 없어 남이 계획을 바꿀 수 없다.

서명 방식(웹페이지 자바스크립트와 같아야 한다):
  key = PBKDF2-SHA256(비밀문구, salt="concert-watch:<owner/repo>", 200000회, 32바이트)
  s   = HMAC-SHA256(key, m)의 16진수, m = JSON 문자열 {"change": ..., "ts": 초, "nonce": "..."}
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time

import requests

from . import picks as picks_store

log = logging.getLogger(__name__)
ITERATIONS = 200_000
CHECK_TEXT = b"concert-watch-check"
MAX_AGE = 48 * 3600          # 이보다 오래된 메시지는 무시(ntfy.sh는 12시간 보관)
NONCE_LIMIT = 500
TIMEOUT = 20


def derive_key(passphrase: str, repo: str) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"),
                               f"concert-watch:{repo}".encode("utf-8"), ITERATIONS, 32)


def check_value(key: bytes) -> str:
    """웹페이지가 비밀문구가 맞는지 바로 알 수 있게 하는 확인값(키 자체는 드러나지 않는다)."""
    return hmac.new(key, CHECK_TEXT, hashlib.sha256).hexdigest()


def sign(key: bytes, m: str) -> str:
    return hmac.new(key, m.encode("utf-8"), hashlib.sha256).hexdigest()


def fetch_messages(server: str, topic: str) -> list[dict]:
    r = requests.get(f"{server.rstrip('/')}/{topic}/json", params={"poll": "1", "since": "all"}, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for line in r.text.splitlines():
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("event") == "message":
            out.append(msg)
    return sorted(out, key=lambda m: m.get("time", 0))


def verify(key: bytes | None, body: str, seen: set, now: float) -> dict:
    """ntfy 메시지 본문을 확인하고 변경 내용을 돌려준다. 받을 수 없으면 ValueError.

    key가 None이면 서명을 확인하지 않는다(config site.edit_passphrase: false).
    """
    try:
        outer = json.loads(body)
        m, s = outer["m"], outer.get("s", "")
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
        raise ValueError("형식이 다른 메시지") from None
    if not isinstance(m, str):
        raise ValueError("형식이 다른 메시지")
    if key is not None and (not isinstance(s, str) or not hmac.compare_digest(sign(key, m), s.lower())):
        raise ValueError("서명이 맞지 않아요(비밀문구가 다르거나 남이 보낸 메시지)")
    inner = json.loads(m)
    nonce, ts = str(inner.get("nonce") or ""), inner.get("ts")
    if not nonce or nonce in seen:
        raise ValueError("이미 반영한 메시지")
    if not isinstance(ts, (int, float)) or abs(now - ts) > MAX_AGE:
        raise ValueError("너무 오래된 메시지")
    change = picks_store.validate_change(inner.get("change"))
    return {"change": change, "nonce": nonce}


def apply_remote(picks: dict, state: dict, passphrase: str | None, repo: str, server: str, topic: str,
                 now: float | None = None, messages: list[dict] | None = None) -> list[str]:
    """ntfy 토픽에 올라온 예매 계획 변경을 picks에 반영하고 결과 문장 목록을 돌려준다.

    passphrase가 None이면 서명 없이 받는다(누구나 바꿀 수 있음).
    """
    now = now or time.time()
    key = derive_key(passphrase, repo) if passphrase is not None else None
    if messages is None:
        messages = fetch_messages(server, topic)
    seen_list = state.setdefault("remote_nonces", [])
    seen = set(seen_list)
    done = []
    for msg in messages:
        try:
            got = verify(key, msg.get("message") or "", seen, now)
        except ValueError as e:
            if "이미 반영" not in str(e):
                log.warning("예매 계획 메시지를 건너뛰어요: %s", e)
            continue
        done.append(picks_store.apply(picks, got["change"]))
        seen.add(got["nonce"])
        seen_list.append(got["nonce"])
    del seen_list[:-NONCE_LIMIT]
    for line in done:
        log.info(line)
    return done
