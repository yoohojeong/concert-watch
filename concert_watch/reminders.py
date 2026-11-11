"""예매 리마인더: '예매할게요'를 고른 공연의 예매 오픈 하루 전과 10분 전에 알린다.

GitHub Actions의 예약 실행은 수십 분 늦어질 수 있어서 정확한 시각을 맞출 수 없다.
그래서 휴대폰 푸시는 ntfy의 예약 발송으로 보낸다. 알릴 시각이 PUSH_WINDOW 안으로 들어오면
그 시각에 도착하도록 ntfy에 미리 맡겨 두고, ntfy 서버가 정확한 시각에 보낸다.
이메일과 문자는 예약이 안 되므로 하루 전 알림에만 쓴다.
"""
from __future__ import annotations

import datetime as dt
import logging

from . import notify, opentimes

log = logging.getLogger(__name__)

KINDS = (("day", dt.timedelta(days=1)), ("10min", dt.timedelta(minutes=10)))
PUSH_WINDOW = dt.timedelta(hours=24)   # 예약은 이 시간 안쪽만(너무 일찍 맡기면 취소할 수 없어서). GitHub 정기 실행이 밀려도 놓치지 않게 넉넉히
SEND_NOW_WITHIN = dt.timedelta(seconds=60)
WEEKDAYS = "월화수목금토일"


def parse_time(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        t = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else None


def fmt_time(t: dt.datetime) -> str:
    return f"{t.month}월 {t.day}일({WEEKDAYS[t.weekday()]}) {t.hour:02d}:{t.minute:02d}"


def fmt_left(delta: dt.timedelta) -> str:
    minutes = max(1, int(delta.total_seconds() // 60))
    days, rem = divmod(minutes, 60 * 24)
    hours, mins = divmod(rem, 60)
    parts = ([f"{days}일"] if days else []) + ([f"{hours}시간"] if hours else []) + ([f"{mins}분"] if mins and not days else [])
    return " ".join(parts)


def _message(pick: dict, kind: str, open_local: dt.datetime, left: dt.timedelta | None,
             label: str = "예매") -> tuple[str, str]:
    artist, title = pick.get("artist", ""), pick.get("title", "")
    if left is None:  # 정시 예약 발송
        when = f"내일 {open_local.hour:02d}:{open_local.minute:02d}" if kind == "day" else "10분 뒤"
        head = f"{artist} {label} {when} 열려요"
    else:             # 늦게 고른 경우 지금 바로 보냄
        head = f"{artist} {label} {fmt_left(left)} 뒤 열려요"
    lines = [title, f"{label} 오픈 {fmt_time(open_local)}"]
    if pick.get("link"):
        lines.append(pick["link"])
    return head, "\n".join(lines)


def _process_slot(cfg, ncfg, key, pick, label, open_at, now, tz, rstate, done_log, dry_run):
    open_local = open_at.astimezone(tz)
    click = pick.get("link") or ""
    for kind, before in KINDS:
        at = open_at - before
        rid = f"{key}|{kind}|{label}|{open_at.isoformat()}"
        rec = rstate.setdefault(rid, {"open_at": open_at.isoformat()})
        late = at <= now + SEND_NOW_WITHIN
        if kind == "day" and late and open_at - now <= dt.timedelta(minutes=15):
            rec["push"] = rec["mail"] = "skipped"   # 10분 전 알림이 곧 가므로 생략
            continue

        # 휴대폰 푸시: 정확한 시각 예약
        if ncfg.get("push") and "push" not in rec:
            if late or at - now <= PUSH_WINDOW:
                head, body = _message(pick, kind, open_local, open_at - now if late else None, label)
                priority = 5 if kind == "10min" else 4
                try:
                    if dry_run:
                        log.info("[미리보기] 푸시 %s: %s / %s", "지금" if late else fmt_time(at.astimezone(tz)), head, body)
                    else:
                        notify.push_raw(head, body, click, at=None if late else at, priority=priority)
                    rec["push"] = "sent" if late else "scheduled"
                    done_log.append(f"{pick.get('artist')} {label} {kind} 푸시 {'보냄' if late else '예약'}")
                except notify.MissingConfig as e:
                    log.warning("푸시 리마인더를 건너뛰었어요: %s 설정이 비어 있어요", e)
                except Exception as e:
                    log.error("푸시 리마인더 실패(%s): %s", rid, e)

        # 이메일·문자: 하루 전 알림만, 시각이 되면(늦어도) 보냄
        if kind == "day" and late and "mail" not in rec:
            head, body = _message(pick, kind, open_local, open_at - now, label)
            sent_any = False
            for channel in ("email", "sms"):
                if not ncfg.get(channel):
                    continue
                try:
                    if dry_run:
                        log.info("[미리보기] %s: %s / %s", channel, head, body)
                    elif channel == "email":
                        notify.send_mail_raw(f"[콘서트 알리미] {head}", f"{head}\n\n{body}")
                    else:
                        notify.send_sms_raw(f"[콘서트 알리미] {head}\n{fmt_time(open_local)}", ncfg)
                    sent_any = True
                except notify.MissingConfig as e:
                    log.warning("%s 리마인더를 건너뛰었어요: %s 설정이 비어 있어요", channel, e)
                except Exception as e:
                    log.error("%s 리마인더 실패(%s): %s", channel, rid, e)
            rec["mail"] = "sent" if sent_any else "none"
            if sent_any:
                done_log.append(f"{pick.get('artist')} {label} 하루 전 이메일·문자 보냄")


def announce_found(cfg: dict, picks: dict, state: dict, now: dt.datetime, dry_run: bool = False) -> int:
    """자동으로 고른 공연에 예매 오픈 일시가 새로 잡히면 한 번 알려준다(고른 뒤에 발견된 것만)."""
    seen = state.setdefault("auto_found", {})
    site_url = (cfg.get("site") or {}).get("url", "")
    count = 0
    for key, pick in sorted(picks.items()):
        if pick.get("mode") != "auto":
            continue
        picked = parse_time(pick.get("picked_at"))
        fresh = []
        for c in opentimes.effective_times(key, pick, state):
            at, found = parse_time(c.get("at")), parse_time(c.get("found_at"))
            fid = f"{key}|{c.get('label')}|{c.get('at', '')[:16]}"
            if not at or at <= now or fid in seen:
                continue
            seen[fid] = now.isoformat(timespec="seconds")
            if picked and found and found <= picked:
                continue      # 고를 때 이미 화면에 보였던 시간
            fresh.append(c)
        if fresh:
            tz = now.tzinfo
            lines = [f"{c.get('label', '예매')} {fmt_time(parse_time(c['at']).astimezone(tz))} ({c.get('source', '')})"
                     for c in fresh]
            text = "\n".join([pick.get("title", "")] + lines + ["하루 전과 10분 전에 알려드릴게요."])
            click = f"{site_url.rstrip('/')}/#{pick['anchor']}" if site_url and pick.get("anchor") else pick.get("link", "")
            notify.broadcast(f"{pick.get('artist', '')} 예매 오픈 일시를 찾았어요", text, click, cfg, dry_run)
            count += 1
    for fid in [f for f, t in seen.items() if (parse_time(t) or now) < now - dt.timedelta(days=120)]:
        del seen[fid]
    return count


def process(cfg: dict, picks: dict, state: dict, now: dt.datetime, dry_run: bool = False) -> list[str]:
    """리마인더를 예약하거나 보내고, 한 일을 문장 목록으로 돌려준다."""
    ncfg = cfg.get("notify") or {}
    tz = now.tzinfo
    rstate = state.setdefault("reminders", {})
    done_log: list[str] = []


    announce_found(cfg, picks, state, now, dry_run)

    for key, pick in sorted(picks.items()):
        for slot in opentimes.effective_times(key, pick, state):
            open_at = parse_time(slot.get("at"))
            if not open_at or open_at <= now:
                continue
            _process_slot(cfg, ncfg, key, pick, slot.get("label") or "예매", open_at, now, tz, rstate, done_log, dry_run)

    # 오픈이 일주일 넘게 지난 기록은 정리
    cutoff = now - dt.timedelta(days=7)
    for rid in [r for r, rec in rstate.items() if (parse_time(rec.get("open_at")) or now) < cutoff]:
        del rstate[rid]
    for line in done_log:
        log.info("리마인더: %s", line)
    return done_log
