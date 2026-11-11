#!/usr/bin/env python3
"""콘서트 알리미: 아티스트의 공연 일정을 확인하고, 새 공연이 뜨면 알림을 보낸다.

    python watch.py               # 확인 + 알림 + 웹페이지 갱신
    python watch.py --dry-run     # 알림을 보내지 않고 내용만 출력
    python watch.py --test-notify # 예시 공연으로 알림 채널 점검
    python watch.py --apply-issue EVENT.json  # 웹페이지에서 연 '예매 계획' 이슈를 data/picks.json에 반영
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from concert_watch import notify, official, opentimes, picks as picks_store, reminders, site, sources

ROOT = Path(__file__).resolve().parent
log = logging.getLogger("concert-watch")


def load_picks(path: Path) -> dict:
    """웹페이지에서 '예매할게요'로 고른 공연(data/picks.json)."""
    return picks_store.load(path)


def apply_issue(event_path: Path, picks_path: Path) -> str:
    """GitHub 이슈 이벤트(JSON)의 예매 계획을 picks.json에 반영한다. 저장소 주인이 연 이슈만 받는다."""
    event = json.loads(event_path.read_text(encoding="utf-8"))
    issue = event.get("issue") or {}
    owner = ((event.get("repository") or {}).get("owner") or {}).get("login")
    author = (issue.get("user") or {}).get("login")
    if not owner or author != owner:
        raise ValueError(f"저장소 주인({owner})이 연 이슈만 반영해요. 작성자: {author}")
    change = picks_store.parse_issue(issue.get("body") or "")
    current = picks_store.load(picks_path)
    result = picks_store.apply(current, change)
    picks_store.save(picks_path, current)
    return result


def load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"events": {}, "artists_seen": []}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def collect(cfg: dict, today: dt.date) -> tuple[list[dict], list[str]]:
    artists = cfg.get("artists") or []
    days = int(cfg.get("lookahead_days", 180))
    events, errors = [], []

    kopis_key = os.environ.get("KOPIS_API_KEY")
    if kopis_key:
        try:
            events += sources.fetch_kopis(artists, today, days, kopis_key)
        except Exception as e:
            errors.append(f"KOPIS({e.__class__.__name__})")
            log.error("KOPIS 조회 실패: %s", e)
    else:
        log.info("KOPIS_API_KEY가 없어 국내 공연 확인을 건너뛰어요")

    tm_key = os.environ.get("TICKETMASTER_API_KEY")
    if tm_key:
        try:
            countries = (cfg.get("ticketmaster") or {}).get("countries") or None
            events += sources.fetch_ticketmaster(artists, today, days, tm_key, countries)
        except Exception as e:
            errors.append(f"Ticketmaster({e.__class__.__name__})")
            log.error("Ticketmaster 조회 실패: %s", e)
    else:
        log.info("TICKETMASTER_API_KEY가 없어 해외 공연 확인을 건너뛰어요")

    return events, errors


def merge(state: dict, events: list[dict], now_iso: str) -> list[dict]:
    """state에 반영하고, 처음 보는 공연 목록을 돌려준다."""
    known = state.setdefault("events", {})
    new = []
    for e in events:
        old = known.get(e["key"])
        if old:
            e["first_seen"] = old.get("first_seen", now_iso)
            if not e.get("ticket_links") and old.get("ticket_links"):
                e["ticket_links"] = old["ticket_links"]
            if not e.get("time") and old.get("time"):
                e["time"] = old["time"]
        else:
            e["first_seen"] = now_iso
            new.append(e)
        known[e["key"]] = e
    return new



def run(cfg: dict, state: dict, now: dt.datetime, events: list[dict] | None, errors: list[str],
        news: list[dict] | None = None, dry_run: bool = False, enrich=None) -> list[dict]:
    """수집 결과를 반영하고 알림을 보낸다. events가 None이면 이번 회차는 공연 API를 건너뛴 것."""
    seen_artists = set(state.get("artists_seen") or [])
    to_notify: list[dict] = []

    if events is not None:
        new = merge(state, events, now.isoformat(timespec="seconds"))
        for e in new:
            if enrich:
                try:
                    enrich(e)
                except Exception as ex:
                    log.warning("상세 정보를 못 가져왔어요(%s): %s", e["key"], ex)

        # 처음 추가한 아티스트는 '지금 있는 공연'을 기준선으로 삼고, 이후 새로 뜨는 공연만 알린다.
        on_first = (cfg.get("notify") or {}).get("on_first_run", False)
        to_notify = sorted(
            (e for e in new if on_first or e["artist"] in seen_artists),
            key=lambda e: (e.get("start", ""), e["artist"]),
        )
        if len(new) > len(to_notify):
            log.info("새로 추가된 아티스트의 기존 공연 %d건은 알림 없이 저장했어요", len(new) - len(to_notify))
        if not errors:  # 소스가 모두 정상일 때만 기준선을 확정
            state["artists_seen"] = sorted(seen_artists | {a["name"] for a in cfg.get("artists") or []})
        state["last_api_check"] = now.isoformat(timespec="seconds")
        state["last_api_errors"] = errors

    store = state.setdefault("announcements", {})
    fresh_news = [n for n in news or [] if n["key"] not in store]
    for n in fresh_news:
        try:
            published = dt.datetime.fromisoformat(n.get("published") or "")
        except ValueError:
            published = now
        if published.tzinfo is None:
            published = published.replace(tzinfo=now.tzinfo)
        n["open_times"] = opentimes.extract(n.get("text") or n.get("summary", ""), n["artist"], published, cfg)
        if n["open_times"]:
            log.info("%s 소식에서 예매 오픈 일시 %d개를 찾았어요", n["artist"], len(n["open_times"]))
        store[n["key"]] = n

    items = fresh_news + to_notify
    if items:
        log.info("%s → 알림 보내는 중", notify.headline(items))
        notify.dispatch(items, cfg, dry_run=dry_run)
    else:
        log.info("새로 뜬 공연이나 공식 소식이 없어요")

    state["last_checked"] = now.isoformat(timespec="seconds")
    prune(state, now.date())
    return items


def prune(state: dict, today: dt.date, keep_days: int = 30) -> None:
    cutoff = (today - dt.timedelta(days=keep_days)).isoformat()
    state["events"] = {
        k: e for k, e in state.get("events", {}).items()
        if (e.get("end") or e.get("start") or "9999") >= cutoff
    }
    state["announcements"] = {
        k: n for k, n in state.get("announcements", {}).items()
        if (n.get("first_seen") or "9999")[:10] >= (today - dt.timedelta(days=90)).isoformat()
    }


def api_due(cfg: dict, state: dict, now: dt.datetime) -> bool:
    last = state.get("last_api_check")
    if not last:
        return True
    hours = float(cfg.get("api_interval_hours", 3))
    return now - dt.datetime.fromisoformat(last) >= dt.timedelta(hours=hours, minutes=-10)


def sample_event(today: dt.date) -> dict:
    d = (today + dt.timedelta(days=40)).isoformat()
    return {
        "key": "test", "source": "test", "artist": "테스트 아티스트", "title": "알림 점검용 예시 공연",
        "start": d, "end": d, "time": "19:00", "venue": "예시 공연장", "city": "서울", "country": "KR",
        "url": "", "ticket_links": [], "sale_start": "",
    }


def _write_output(name: str, value: str) -> None:
    """GitHub Actions 단계 출력(이슈 댓글에 쓴다)."""
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"{name}={value.replace(chr(10), ' ')}\n")


def main() -> int:
    p = argparse.ArgumentParser(description="콘서트 알리미")
    p.add_argument("--config", default=str(ROOT / "config.yaml"))
    p.add_argument("--dry-run", action="store_true", help="알림을 보내지 않고 내용만 출력")
    p.add_argument("--test-notify", action="store_true", help="예시 공연으로 알림 채널 점검")
    p.add_argument("--reminders-only", action="store_true", help="공연 확인 없이 예매 리마인더만 처리")
    p.add_argument("--apply-issue", metavar="EVENT_JSON", help="예매 계획 이슈를 반영한 뒤 리마인더 처리")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
    now = dt.datetime.now(ZoneInfo(cfg.get("timezone", "Asia/Seoul")))

    if args.test_notify:
        results = notify.dispatch([sample_event(now.date())], cfg, dry_run=args.dry_run)
        log.info("점검 결과: %s", results or "켜진 알림 채널이 없어요(config.yaml의 notify 확인)")
        return 0 if results and "failed" not in results.values() else 1

    state_path = ROOT / "data" / "state.json"
    state = load_state(state_path)
    picks_path = ROOT / "data" / "picks.json"
    if args.apply_issue:
        try:
            result = apply_issue(Path(args.apply_issue), picks_path)
        except (ValueError, KeyError, json.JSONDecodeError) as e:
            log.error("예매 계획을 반영하지 못했어요: %s", e)
            _write_output("result", f"반영하지 못했어요: {e}")
            return 1
        log.info(result)
        _write_output("result", result)
    picks = load_picks(picks_path)

    if args.reminders_only or args.apply_issue:
        reminders.process(cfg, picks, state, now, dry_run=args.dry_run)
        save_state(state_path, state)
        site.render(state, cfg, now, ROOT / "docs", picks=picks)
        return 0

    events, errors = None, []
    if api_due(cfg, state, now):
        events, errors = collect(cfg, now.date())
    else:
        log.info("공연 API는 %s시간마다 확인해요. 이번에는 공식 채널만 확인할게요",
                 cfg.get("api_interval_hours", 3))
    news, news_errors = official.check(cfg, state, now)

    kopis_key = os.environ.get("KOPIS_API_KEY")
    enrich = (lambda e: sources.enrich_kopis(e, kopis_key)
              if e["source"] == "kopis" else None) if kopis_key else None

    run(cfg, state, now, events, errors, news=news, dry_run=args.dry_run, enrich=enrich)
    reminders.process(cfg, picks, state, now, dry_run=args.dry_run)
    state["last_errors"] = (state.get("last_api_errors") or []) + news_errors
    save_state(state_path, state)
    site.render(state, cfg, now, ROOT / "docs", picks=picks)

    for err in state["last_errors"]:
        print(f"::warning::확인 실패 {err}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
