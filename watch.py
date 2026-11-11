#!/usr/bin/env python3
"""콘서트 알리미: 아티스트의 공연 일정을 확인하고, 새 공연이 뜨면 알림을 보낸다.

    python watch.py               # 확인 + 알림 + 웹페이지 갱신
    python watch.py --dry-run     # 알림을 보내지 않고 내용만 출력
    python watch.py --test-notify # 예시 공연으로 알림 채널 점검
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

from concert_watch import artists as artists_store, notify, official, opentimes, picks as picks_store, reminders, remote, site, sources

ROOT = Path(__file__).resolve().parent
log = logging.getLogger("concert-watch")


def load_picks(path: Path) -> dict:
    """웹페이지에서 '예매할게요'로 고른 공연(data/picks.json)."""
    return picks_store.load(path)


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
        news: list[dict] | None = None, dry_run: bool = False, enrich=None,
        hourly_events: list[dict] | None = None) -> list[dict]:
    """수집 결과를 반영하고 알림을 보낸다. events가 None이면 이번 회차는 공연 API를 건너뛴 것."""
    seen_artists = set(state.get("artists_seen") or [])
    to_notify: list[dict] = []

    # 매시간 확인하는 소스(멜론티켓 공지). 이미 추적하던 아티스트의 새 공연은 바로 알린다.
    if hourly_events:
        for e in merge(state, hourly_events, now.isoformat(timespec="seconds")):
            if (cfg.get("notify") or {}).get("on_first_run", False) or e["artist"] in seen_artists:
                to_notify.append(e)

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
            to_notify + [e for e in new if on_first or e["artist"] in seen_artists],
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


CHECK_INTERVAL = dt.timedelta(minutes=55)


def check_due(state: dict, now: dt.datetime) -> bool:
    """정기 확인(예매처 공지·뉴스·공식 사이트·리마인더)을 할 때가 됐는지. 약 1시간마다."""
    last = state.get("last_checked")
    return not last or now - dt.datetime.fromisoformat(last) >= CHECK_INTERVAL


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


def apply_remote_edits(cfg: dict, state: dict, all_picks: dict, artists_data: dict | None = None,
                       added_artists: list[str] | None = None) -> bool:
    """웹페이지에서 보낸 예매 계획·아티스트 변경(ntfy)을 반영한다. 바뀐 게 있으면 True."""
    site_cfg = cfg.get("site") or {}
    topic = site_cfg.get("edit_topic")
    repo = os.environ.get("GITHUB_REPOSITORY") or site_cfg.get("repo", "")
    if site_cfg.get("edit_passphrase", True):
        passphrase = os.environ.get("PICK_PASSPHRASE")
        if not passphrase:
            return False
    else:
        passphrase = None          # 비밀문구 없이 누구나 바꿀 수 있음
    if not (topic and repo):
        return False
    try:
        return bool(remote.apply_remote(all_picks, state, passphrase, repo,
                                        site_cfg.get("ntfy_server") or "https://ntfy.sh", topic,
                                        artists_data=artists_data, cfg=cfg, added_artists=added_artists))
    except Exception as e:
        log.error("웹페이지에서 보낸 예매 계획 변경을 읽지 못했어요: %s", e)
        return False


def main() -> int:
    p = argparse.ArgumentParser(description="콘서트 알리미")
    p.add_argument("--config", default=str(ROOT / "config.yaml"))
    p.add_argument("--dry-run", action="store_true", help="알림을 보내지 않고 내용만 출력")
    p.add_argument("--test-notify", action="store_true", help="예시 공연으로 알림 채널 점검")
    p.add_argument("--force-api", action="store_true", help="확인 간격과 상관없이 공연 API(KOPIS·Ticketmaster)를 지금 확인")
    p.add_argument("--auto", action="store_true",
                   help="5분마다: 웹페이지 변경은 바로 반영하고, 공연 확인은 1시간마다만")
    p.add_argument("--reminders-only", action="store_true", help="공연 확인 없이 예매 리마인더만 처리")
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
    all_picks = load_picks(picks_path)
    artists_path = ROOT / "data" / "artists.json"
    artists_data = artists_store.load(artists_path)
    base_artists = list(cfg.get("artists") or [])
    cfg["artists"] = artists_store.merged({"artists": base_artists}, artists_data)
    before = json.dumps(artists_data, sort_keys=True)
    new_artists: list[str] = []
    picks_changed = apply_remote_edits(cfg, state, all_picks, artists_data, new_artists)
    artists_changed = json.dumps(artists_data, sort_keys=True) != before
    if artists_changed:
        artists_store.save(artists_path, artists_data)
        cfg["artists"] = artists_store.merged({"artists": base_artists}, artists_data)

    quick = False
    if args.auto and not (new_artists or check_due(state, now)):
        if not (picks_changed or artists_changed):
            log.info("웹페이지에서 바뀐 게 없고, 공연 확인은 아직 때가 아니에요")
            return 0
        quick = True       # 예매 계획·아티스트 변경만 바로 반영

    if args.reminders_only or quick:
        if picks_changed:
            picks_store.save(picks_path, all_picks)
        picks = picks_store.active(all_picks)
        reminders.process(cfg, picks, state, now, dry_run=args.dry_run)
        save_state(state_path, state)
        site.render(state, cfg, now, ROOT / "docs", picks=picks)
        return 0

    events, errors = None, []
    if new_artists:
        log.info("새 아티스트 %s → 모든 소스를 지금 확인해요", ", ".join(new_artists))
    if args.force_api or new_artists or api_due(cfg, state, now):
        events, errors = collect(cfg, now.date())
    else:
        log.info("공연 API는 %s시간마다 확인해요. 이번에는 공식 채널만 확인할게요",
                 cfg.get("api_interval_hours", 3))
    news, news_errors = official.check(cfg, state, now)
    # 예매처 티켓오픈 공지(매시간). 한 곳이 실패해도 나머지는 계속한다.
    melon: list[dict] = []
    artists = cfg.get("artists") or []
    for label, fetch in [("멜론티켓", lambda: sources.fetch_melon(artists, state)),
                         ("인터파크", lambda: sources.fetch_interpark(artists)),
                         ("예스24", lambda: sources.fetch_yes24(artists, state))]:
        try:
            melon += fetch()
        except Exception as e:
            news_errors.append(f"{label}({e.__class__.__name__})")
            log.error("%s 티켓오픈 공지 확인 실패: %s", label, e)
    # 새로 추가한 아티스트는 이미 지나간 공지도 거슬러 올라가 찾는다
    fresh = [a for a in artists if a["name"] in new_artists]
    if fresh:
        for label, fetch in [("멜론티켓", lambda: sources.fetch_melon(fresh, {})),
                             ("예스24", lambda: sources.fetch_yes24(fresh, {}))]:
            try:
                melon += fetch()
            except Exception as e:
                log.error("%s 지난 공지 확인 실패: %s", label, e)

    kopis_key = os.environ.get("KOPIS_API_KEY")
    enrich = (lambda e: sources.enrich_kopis(e, kopis_key)
              if e["source"] == "kopis" else None) if kopis_key else None

    run(cfg, state, now, events, errors, news=news, dry_run=args.dry_run, enrich=enrich, hourly_events=melon)
    added = picks_store.auto_add(all_picks, state, cfg, now)
    if added:
        log.info("예매 오픈 시각이 나온 공연 %d건을 예매 계획에 자동으로 넣었어요: %s", len(added), ", ".join(added))
    if picks_changed or added:
        picks_store.save(picks_path, all_picks)
    picks = picks_store.active(all_picks)
    reminders.process(cfg, picks, state, now, dry_run=args.dry_run)
    state["last_errors"] = (state.get("last_api_errors") or []) + news_errors
    save_state(state_path, state)
    site.render(state, cfg, now, ROOT / "docs", picks=picks)

    for err in state["last_errors"]:
        print(f"::warning::확인 실패 {err}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
