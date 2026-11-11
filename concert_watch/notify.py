"""알림 채널: 이메일(SMTP), 문자(Solapi 또는 Twilio), 휴대폰 푸시(ntfy).

비밀번호·전화번호·이메일 주소는 공개 저장소에 남지 않도록 모두 환경변수(GitHub Secrets)로 받는다.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import logging
import os
import secrets
import smtplib
import ssl
from email.message import EmailMessage
from html import escape
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger(__name__)
WEEKDAYS = "월화수목금토일"
TIMEOUT = 20


class MissingConfig(Exception):
    pass


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name) or default
    if not value:
        raise MissingConfig(name)
    return value


def _env_list(name: str) -> list[str]:
    items = [x.strip() for x in os.environ.get(name, "").split(",") if x.strip()]
    if not items:
        raise MissingConfig(name)
    return items


def _fmt_date(value: str, short: bool = False) -> str:
    try:
        d = dt.date.fromisoformat(value)
    except ValueError:
        return value
    if short:
        return f"{d.month}.{d.day}"
    return f"{d.year}.{d.month}.{d.day}({WEEKDAYS[d.weekday()]})"


def date_label(e: dict, short: bool = False) -> str:
    s, en = e.get("start", ""), e.get("end", "")
    if not en or en == s:
        return _fmt_date(s, short)
    return f"{_fmt_date(s, short)}~{_fmt_date(en, short)}"


def place_label(e: dict) -> str:
    return ", ".join(p for p in (e.get("venue"), e.get("city")) if p)


def anchor_id(key: str) -> str:
    """웹페이지에서 공연·소식을 가리키는 짧은 id(알림 링크의 #뒤에 붙는다)."""
    return "e-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]


def pick_link(e: dict, site_url: str) -> str:
    return f"{site_url.rstrip('/')}/#{anchor_id(e['key'])}" if site_url and e.get("key") else ""


def _main_link(e: dict) -> str:
    links = e.get("ticket_links") or []
    return links[0]["url"] if links else e.get("url", "")


# ── 메시지 만들기 ───────────────────────────────────────
def is_news(e: dict) -> bool:
    return e.get("kind") == "announcement"


def headline(items: list[dict]) -> str:
    news = sum(1 for e in items if is_news(e))
    shows = len(items) - news
    parts = ([f"새 공연 {shows}건"] if shows else []) + ([f"공식 소식 {news}건"] if news else [])
    return ", ".join(parts) or "새 소식"


def _source_name(e: dict) -> str:
    return {"instagram": "공식 인스타그램", "web": "공식 사이트"}.get(e.get("source"), "공식 채널")


def open_time_lines(e: dict) -> list[str]:
    out = []
    for c in e.get("open_times") or []:
        try:
            t = dt.datetime.fromisoformat(c["at"]).astimezone(ZoneInfo("Asia/Seoul"))
        except (KeyError, ValueError):
            continue
        out.append(f"{c.get('label', '예매')} {t.month}.{t.day}({WEEKDAYS[t.weekday()]}) {t.hour:02d}:{t.minute:02d}")
    return out


def build_text(events: list[dict], site_url: str) -> str:
    lines = [f"{headline(events)}이 떴어요."]
    for e in events:
        if is_news(e):
            lines += ["", f"[{e['artist']}] {_source_name(e)}", e["title"]]
            if e.get("summary") and e["summary"] != e["title"]:
                lines.append(e["summary"])
            if open_time_lines(e):
                lines.append("찾은 예매 오픈 일시(한국 시간): " + ", ".join(open_time_lines(e)))
            if e.get("url"):
                lines.append(e["url"])
            if pick_link(e, site_url):
                lines.append(f"예매할지 고르기: {pick_link(e, site_url)}")
            continue
        lines += ["", f"[{e['artist']}] {e['title']}", f"{date_label(e)}  {place_label(e)}"]
        if e.get("sale_start"):
            lines.append(f"예매 오픈: {e['sale_start']}")
        for link in e.get("ticket_links") or []:
            lines.append(f"{link['name']}: {link['url']}")
        if pick_link(e, site_url):
            lines.append(f"예매할지 고르기: {pick_link(e, site_url)}")
    if site_url:
        lines += ["", f"전체 일정 보기: {site_url}"]
    return "\n".join(lines)


def build_html(events: list[dict], site_url: str) -> str:
    rows = []
    for e in events:
        if is_news(e):
            link = f'<a href="{escape(e["url"])}">원문 보기</a>' if e.get("url") else ""
            if pick_link(e, site_url):
                link += f' &nbsp; <a href="{escape(pick_link(e, site_url))}">예매할지 고르기</a>'
            rows.append(
                f'<div style="margin:0 0 20px">'
                f'<div style="color:#C21E56;font-weight:700">{escape(e["artist"])} {escape(_source_name(e))}</div>'
                f'<div style="font-size:17px;font-weight:700">{escape(e["title"])}</div>'
                f'<div style="color:#555">{escape(e.get("summary", ""))}</div>'
                f'<div style="margin-top:4px">{link}</div></div>'
            )
            continue
        links = " ".join(
            f'<a href="{escape(l["url"])}">{escape(l["name"])}</a>' for l in e.get("ticket_links") or []
        )
        if pick_link(e, site_url):
            links += f' &nbsp; <a href="{escape(pick_link(e, site_url))}">예매할지 고르기</a>'
        sale = f"<br>예매 오픈: {escape(e['sale_start'])}" if e.get("sale_start") else ""
        rows.append(
            f'<div style="margin:0 0 20px">'
            f'<div style="color:#C21E56;font-weight:700">{escape(e["artist"])}</div>'
            f'<div style="font-size:17px;font-weight:700">{escape(e["title"])}</div>'
            f'<div style="color:#555">{escape(date_label(e))} &nbsp; {escape(place_label(e))}{sale}</div>'
            f'<div style="margin-top:4px">{links}</div></div>'
        )
    footer = f'<p><a href="{escape(site_url)}">전체 일정 보기</a></p>' if site_url else ""
    return (f'<div style="font-family:sans-serif;line-height:1.5">'
            f'<p>{escape(headline(events))}이 떴어요.</p>{"".join(rows)}{footer}</div>')


def _sms_line(e: dict) -> str:
    if is_news(e):
        return f"{e['artist']} {_source_name(e)}: {e['title'][:30]}"
    return f"{e['artist']} {date_label(e, short=True)} {e.get('venue') or e.get('city')}"


def build_sms(events: list[dict], site_url: str) -> str:
    lines = [f"[콘서트 알리미] {headline(events)}"]
    lines += [_sms_line(e) for e in events[:3]]
    if len(events) > 3:
        lines.append(f"외 {len(events) - 3}건")
    if site_url:
        lines.append(site_url)
    return "\n".join(lines)


def _subject(events: list[dict]) -> str:
    artists = list(dict.fromkeys(e["artist"] for e in events))
    head = ", ".join(artists[:2]) + (f" 외 {len(artists) - 2}팀" if len(artists) > 2 else "")
    return f"[콘서트 알리미] {head} {headline(events)}"


# ── 채널 ──────────────────────────────────────────────
def send_mail_raw(subject: str, text: str, html: str | None = None) -> None:
    host = _env("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT") or 465)
    user, password = _env("SMTP_USER"), _env("SMTP_PASSWORD")
    to = _env_list("NOTIFY_EMAIL_TO")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ.get("EMAIL_FROM") or user
    msg["To"] = ", ".join(to)
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")

    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=TIMEOUT) as s:
            s.login(user, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=TIMEOUT) as s:
            s.starttls(context=ctx)
            s.login(user, password)
            s.send_message(msg)


def send_email(events: list[dict], site_url: str, cfg: dict) -> None:
    send_mail_raw(_subject(events), build_text(events, site_url), build_html(events, site_url))


def _solapi_auth(key: str, secret: str) -> str:
    date = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    salt = secrets.token_hex(16)
    sig = hmac.new(secret.encode(), (date + salt).encode(), hashlib.sha256).hexdigest()
    return f"HMAC-SHA256 apiKey={key}, date={date}, salt={salt}, signature={sig}"


def send_sms(events: list[dict], site_url: str, cfg: dict) -> None:
    send_sms_raw(build_sms(events, site_url), cfg)


def send_sms_raw(text: str, cfg: dict) -> None:
    sender = _env("SMS_FROM")
    recipients = _env_list("NOTIFY_SMS_TO")
    provider = (cfg.get("sms_provider") or "solapi").lower()

    for to in recipients:
        if provider == "solapi":
            key, secret = _env("SOLAPI_API_KEY"), _env("SOLAPI_API_SECRET")
            r = requests.post(
                "https://api.solapi.com/messages/v4/send",
                headers={"Authorization": _solapi_auth(key, secret)},
                json={"message": {"to": to.replace("-", ""), "from": sender.replace("-", ""), "text": text}},
                timeout=TIMEOUT,
            )
        elif provider == "twilio":
            sid, token = _env("TWILIO_ACCOUNT_SID"), _env("TWILIO_AUTH_TOKEN")
            r = requests.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
                data={"To": to, "From": sender, "Body": text},
                auth=(sid, token), timeout=TIMEOUT,
            )
        else:
            raise MissingConfig(f"지원하지 않는 sms_provider: {provider}")
        if r.status_code >= 400:
            raise RuntimeError(f"{provider} {r.status_code}: {r.text[:300]}")


def push_raw(title: str, message: str, click: str = "", at: dt.datetime | None = None,
             priority: int | None = None) -> None:
    """ntfy(https://ntfy.sh) 앱으로 휴대폰 푸시를 보낸다. at을 주면 그 시각에 도착하도록 예약한다(최대 3일 뒤)."""
    topic = _env("NTFY_TOPIC")
    server = (os.environ.get("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")
    headers = {}
    if os.environ.get("NTFY_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['NTFY_TOKEN']}"
    body = {"topic": topic, "title": title, "message": message, "tags": ["tickets"]}
    if click:
        body["click"] = click
    if priority:
        body["priority"] = priority
    if at is not None:
        body["delay"] = str(int(at.timestamp()))
    r = requests.post(server, json=body, headers=headers, timeout=TIMEOUT)
    if r.status_code >= 400:
        raise RuntimeError(f"ntfy {r.status_code}: {r.text[:300]}")


def send_push(events: list[dict], site_url: str, cfg: dict) -> None:
    if len(events) > 5:
        push_raw(_subject(events), build_sms(events, ""), site_url)
        return
    for e in events:
        if is_news(e):
            title, message = f"{e['artist']} {_source_name(e)}", e.get("summary") or e["title"]
        else:
            title, message = f"{e['artist']} 새 공연", f"{e['title']}\n{date_label(e)}  {place_label(e)}"
        # 누르면 웹페이지의 해당 공연으로 가서 바로 예매 여부를 고를 수 있게 한다.
        push_raw(title, message, pick_link(e, site_url) or _main_link(e))


def broadcast(title: str, text: str, click: str, cfg: dict, dry_run: bool = False) -> list[str]:
    """켜진 채널 모두로 짧은 안내를 보낸다(리마인더 외의 안내용)."""
    ncfg = cfg.get("notify") or {}
    sent = []
    for ch in ("push", "email", "sms"):
        if not ncfg.get(ch):
            continue
        try:
            if dry_run:
                log.info("[미리보기] %s: %s / %s", ch, title, text)
            elif ch == "push":
                push_raw(title, text, click)
            elif ch == "email":
                send_mail_raw(f"[콘서트 알리미] {title}", f"{title}\n\n{text}" + (f"\n\n{click}" if click else ""))
            else:
                send_sms_raw(f"[콘서트 알리미] {title}", ncfg)
            sent.append(ch)
        except MissingConfig as e:
            log.warning("%s 안내를 건너뛰었어요: %s 설정이 비어 있어요", ch, e)
        except Exception as e:
            log.error("%s 안내 실패: %s", ch, e)
    return sent


CHANNELS = {"email": send_email, "sms": send_sms, "push": send_push}


def dispatch(events: list[dict], cfg: dict, dry_run: bool = False) -> dict[str, str]:
    ncfg = cfg.get("notify") or {}
    site_url = (cfg.get("site") or {}).get("url", "")
    results = {}
    for name, fn in CHANNELS.items():
        if not ncfg.get(name):
            continue
        if dry_run:
            preview = build_sms(events, site_url) if name == "sms" else build_text(events, site_url)
            log.info("[미리보기] %s 알림\n%s", name, preview)
            results[name] = "dry-run"
            continue
        try:
            fn(events, site_url, ncfg)
            results[name] = "sent"
            log.info("%s 알림을 보냈어요", name)
        except MissingConfig as e:
            results[name] = "skipped"
            log.warning("%s 알림을 건너뛰었어요: %s 설정이 비어 있어요", name, e)
        except Exception as e:  # 한 채널이 실패해도 나머지는 보낸다
            results[name] = "failed"
            log.error("%s 알림 실패: %s", name, e)
    return results
