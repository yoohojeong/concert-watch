"""수집한 공연을 GitHub Pages용 웹페이지(docs/index.html)로 만든다.

페이지에서 '예매할게요'를 저장하면 변경 내용을 비밀문구로 서명해 ntfy 토픽에 올린다. GitHub Actions가 다음 실행 때
서명을 확인하고 data/picks.json에 반영한다(concert_watch/remote.py). GitHub 로그인이나 토큰은 필요 없다.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from collections import Counter
from html import escape
from pathlib import Path

from . import opentimes, remote
from .notify import anchor_id

WEEKDAYS = "월화수목금토일"
NEW_DAYS = 7     # 처음 발견된 지 며칠까지 '새로 떴어요' 표시
NEWS_DAYS = 30   # 최근 공식 소식을 며칠치 보여줄지
SOURCE_NAMES = {"instagram": "공식 인스타그램", "web": "공식 사이트", "news": "뉴스"}


def _safe_url(url: str) -> str:
    return escape(url) if (url or "").startswith(("https://", "http://")) else ""


def _date(value: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _md(d: dt.date) -> str:
    return f"{d.month}월 {d.day}일"


def upcoming(events: list[dict], today: dt.date) -> list[dict]:
    out = []
    for e in events:
        start, end = _date(e.get("start")), _date(e.get("end")) or _date(e.get("start"))
        if start and end and end >= today:
            out.append({**e, "_start": start, "_end": end, "_shown": max(start, today)})
    return sorted(out, key=lambda e: (e["_shown"], e["artist"], e["title"]))


def recent_news(state: dict, now: dt.datetime) -> list[dict]:
    cutoff = now - dt.timedelta(days=NEWS_DAYS)
    out = []
    for n in state.get("announcements", {}).values():
        try:
            seen = dt.datetime.fromisoformat(n.get("first_seen", ""))
        except ValueError:
            continue
        if seen >= cutoff:
            out.append({**n, "_seen": seen})
    return sorted(out, key=lambda n: n["_seen"], reverse=True)[:12]


def _pick_controls(key: str) -> str:
    return (f'<div class="pick-row"><button type="button" class="pick" data-key="{escape(key)}">예매할게요</button>'
            f'<button type="button" class="unpick" data-key="{escape(key)}" hidden>예매 안 할래요</button>'
            f'<p class="pick-state" data-key="{escape(key)}" hidden></p></div>')


def _row(e: dict, now: dt.datetime) -> str:
    d, end = e["_start"], e["_end"]
    when = _md(d) if end == d else f"{_md(d)} ~ {_md(end)}"
    if e.get("time") and len(e["time"]) <= 5:
        when += f" {e['time']}"
    place = ", ".join(escape(p) for p in (e.get("venue"), e.get("city")) if p)

    links = [l for l in e.get("ticket_links") or [] if _safe_url(l.get("url", ""))]
    if not links and _safe_url(e.get("url", "")):
        links = [{"name": "공연 정보", "url": e["url"]}]
    link_html = "".join(
        f'<a class="ticket" href="{_safe_url(l["url"])}" target="_blank" rel="noopener">{escape(l["name"])}</a>'
        for l in links
    )

    is_new = False
    try:
        is_new = now - dt.datetime.fromisoformat(e.get("first_seen", "")) <= dt.timedelta(days=NEW_DAYS)
    except ValueError:
        pass

    sale = ""
    if e.get("sale_start"):
        sale = f'<p class="sale" data-utc="{escape(e["sale_start"])}">일반 예매 오픈 {escape(e["sale_start"][:16].replace("T", " "))} UTC</p>'
    return f"""
      <li class="show" id="{anchor_id(e['key'])}" data-artist="{escape(e['artist'])}">
        <div class="day" aria-hidden="true"><span class="num">{d.day}</span><span class="wd">{WEEKDAYS[d.weekday()]}</span></div>
        <div class="body">
          <p class="artist">{escape(e['artist'])}{'<span class="new">새로 떴어요</span>' if is_new else ''}</p>
          <h3>{escape(e['title'])}</h3>
          <p class="meta">{escape(when)}{' &nbsp;' + place if place else ''}</p>
          {sale}
          <div class="links">{link_html}</div>
          {_pick_controls(e['key'])}
        </div>
      </li>"""


def _news_item(n: dict) -> str:
    s = n["_seen"]
    link = (f'<a class="ticket" href="{_safe_url(n["url"])}" target="_blank" rel="noopener">원문 보기</a>'
            if _safe_url(n.get("url", "")) else "")
    summary = n.get("summary", "")
    body = f'<p class="meta">{escape(summary)}</p>' if summary and summary != n.get("title") else ""
    return f"""
      <li class="show news" id="{anchor_id(n['key'])}" data-artist="{escape(n['artist'])}">
        <div class="day" aria-hidden="true"><span class="num">{s.day}</span><span class="wd">{s.month}월</span></div>
        <div class="body">
          <p class="artist">{escape(n['artist'])} {escape(SOURCE_NAMES.get(n.get('source'), '공식 채널'))}</p>
          <h3>{escape(n.get('title', ''))}</h3>
          {body}
          <div class="links">{link}</div>
          {_pick_controls(n['key'])}
        </div>
      </li>"""


def _edit_info(site: dict) -> dict:
    """웹페이지에서 예매 계획을 바꿀 때 쓰는 ntfy 토픽과 비밀문구 확인값."""
    repo = os.environ.get("GITHUB_REPOSITORY") or site.get("repo", "")
    need = bool(site.get("edit_passphrase", True))
    passphrase = os.environ.get("PICK_PASSPHRASE")
    check = remote.check_value(remote.derive_key(passphrase, repo)) if need and passphrase and repo else ""
    return {"topic": site.get("edit_topic", ""), "server": (site.get("ntfy_server") or "https://ntfy.sh").rstrip("/"),
            "passphrase": need, "check": check}


def _page_data(shows: list[dict], news: list[dict], picks: dict, cfg: dict, state: dict) -> str:
    items = {}
    for e in shows:
        links = [l["url"] for l in e.get("ticket_links") or [] if _safe_url(l.get("url", ""))]
        items[e["key"]] = {"artist": e["artist"], "title": e["title"], "anchor": anchor_id(e["key"]),
                           "country": e.get("country", ""), "start": e.get("start", ""),
                           "link": (links or [e.get("url", "")])[0]}
    for n in news:
        items[n["key"]] = {"artist": n["artist"], "title": n.get("title", ""), "anchor": anchor_id(n["key"]),
                           "country": "", "start": "", "link": ""}
    times = {}
    for key in set(items) | set(picks):
        info = picks.get(key) or items.get(key) or {}
        found = opentimes.candidates_for(key, info, state)
        if found:
            times[key] = [{"label": c.get("label", "예매"), "at": c["at"], "source": c.get("source", ""),
                           "source_url": c.get("source_url", "")} for c in found]
    site = cfg.get("site") or {}
    data = {
        "repo": os.environ.get("GITHUB_REPOSITORY") or site.get("repo", ""),
        "branch": os.environ.get("GITHUB_REF_NAME") or site.get("branch", "main"),
        "items": items,
        "times": times,
        "picks": picks,
        "edit": _edit_info(site),
    }
    # </script> 안전 처리
    return json.dumps(data, ensure_ascii=False).replace("</", "<\\/")


STYLE = """
:root {
  color-scheme: light;
  --bg: #F2F1F7; --surface: #FFFFFF; --ink: #1F1A33; --muted: #6A6585;
  --accent: #C21E56; --accent-ink: #FFFFFF; --accent-soft: #F8DCE6; --link: #2F6F8F; --line: #DAD6E6;
  --display: "Black Han Sans", "Apple SD Gothic Neo", "Malgun Gothic", sans-serif;
  --text: "IBM Plex Sans KR", "Apple SD Gothic Neo", "Malgun Gothic", system-ui, sans-serif;
  box-sizing: border-box;
  padding-top: env(safe-area-inset-top, 0px);
  padding-bottom: env(safe-area-inset-bottom, 0px);
}
@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;
    --bg: #17142A; --surface: #211D38; --ink: #EEEAF8; --muted: #A39DBF;
    --accent: #FF6B9A; --accent-ink: #1A0D14; --accent-soft: #4A1D33; --link: #7CC3E6; --line: #332E4F;
  }
}
*, *::before, *::after { box-sizing: inherit; }
body { margin: 0; background: var(--bg); color: var(--ink); font: 16px/1.6 var(--text); }
.wrap { max-width: 46rem; margin: 0 auto; padding: 3rem 1.25rem 4rem; }
header h1 { font: 400 clamp(2.6rem, 9vw, 4.4rem)/1 var(--display); margin: 0 0 .9rem; letter-spacing: -.01em; }
header p { margin: 0; color: var(--muted); }
.warn { margin-top: .75rem !important; color: var(--accent) !important; font-size: .9rem; }
.status { margin: 1rem 0 0; padding: .75rem 1rem; border-radius: 8px; background: var(--accent-soft); color: var(--ink); }
.status:empty { display: none; }
.filters { display: flex; flex-wrap: wrap; gap: .5rem; margin: 2rem 0 .5rem; }
.chip { font: 600 .9rem/1 var(--text); color: var(--ink); background: transparent;
  border: 1.5px solid var(--line); border-radius: 999px; padding: .55rem .9rem; cursor: pointer; }
.chip[aria-pressed="true"] { background: var(--ink); color: var(--bg); border-color: var(--ink); }
button:focus-visible, a:focus-visible, input:focus-visible { outline: 3px solid var(--accent); outline-offset: 2px; }
.month h2 { font: 400 1.6rem/1.2 var(--display); margin: 2.75rem 0 .75rem; padding-bottom: .5rem;
  border-bottom: 2px solid var(--ink); }
ol { list-style: none; margin: 0; padding: 0; }
.show { display: grid; grid-template-columns: 4.25rem 1fr; gap: 1rem; padding: 1.1rem 0;
  border-bottom: 1px solid var(--line); scroll-margin-top: calc(env(safe-area-inset-top, 0px) + 1rem); }
.show[hidden] { display: none; }
.show.focus { background: var(--accent-soft); border-radius: 8px; padding-left: .5rem; padding-right: .5rem; }
.day { text-align: center; }
.day .num { display: block; font: 400 2.75rem/1 var(--display); color: var(--accent); }
.day .wd { display: block; color: var(--muted); font-size: .85rem; margin-top: .25rem; }
.artist { margin: 0; font-weight: 600; font-size: .9rem; color: var(--muted); }
.new { display: inline-block; margin-left: .5rem; padding: .05rem .5rem; border-radius: 4px;
  background: var(--accent-soft); color: var(--accent); font-size: .8rem; }
.show h3 { margin: .15rem 0 .2rem; font-size: 1.1rem; line-height: 1.4; overflow-wrap: anywhere; }
.meta, .sale { margin: 0; color: var(--muted); font-size: .95rem; overflow-wrap: anywhere; }
.links { display: flex; flex-wrap: wrap; gap: .25rem 1rem; margin-top: .4rem; }
.ticket { color: var(--link); font-weight: 600; font-size: .95rem; text-decoration: underline; text-underline-offset: 3px; }
.pick-row { display: flex; flex-wrap: wrap; align-items: center; gap: .5rem .9rem; margin-top: .7rem; }
.pick { font: 600 .9rem/1 var(--text); padding: .6rem 1rem; border-radius: 6px; cursor: pointer;
  color: var(--accent); background: transparent; border: 1.5px solid var(--accent); }
.pick[aria-pressed="true"] { background: var(--accent); color: var(--accent-ink); }
.unpick { font: 600 .9rem/1 var(--text); padding: .6rem 1rem; border-radius: 6px; cursor: pointer;
  color: var(--muted); background: transparent; border: 1.5px solid var(--line); }
.unpick[hidden] { display: none; }
.unpick.confirm { color: var(--accent-ink); background: var(--accent); border-color: var(--accent); }
.unpick:disabled { opacity: .6; cursor: progress; }
.pick-state { margin: 0; font-size: .9rem; color: var(--ink); }
.plan h2 { border-bottom-color: var(--accent); }
.plan .left { margin: .2rem 0 0; font-weight: 600; color: var(--accent); }
.empty { margin-top: 3rem; padding: 1.5rem; background: var(--surface); border-radius: 8px; }
footer { margin-top: 3rem; color: var(--muted); font-size: .85rem; }
footer button { font: inherit; color: var(--link); background: none; border: 0; padding: 0; text-decoration: underline; cursor: pointer; }
dialog { width: min(32rem, calc(100vw - 2rem)); border: 0; border-radius: 12px; padding: 1.5rem;
  background: var(--surface); color: var(--ink); }
dialog::backdrop { background: rgba(23, 20, 42, .55); }
dialog h2 { font: 400 1.5rem/1.25 var(--display); margin: 0 0 .25rem; }
dialog .sub { margin: 0 0 1.25rem; color: var(--muted); }
.field { margin: 0 0 1.1rem; }
.field label { display: block; font-weight: 600; margin-bottom: .35rem; }
.field input { width: 100%; font: 1rem var(--text); padding: .65rem .75rem; border-radius: 6px;
  border: 1.5px solid var(--line); background: var(--bg); color: var(--ink); }
.field .help { margin: .35rem 0 0; font-size: .85rem; color: var(--muted); }
fieldset.field { border: 0; padding: 0; min-width: 0; }
fieldset.field legend { font-weight: 600; margin-bottom: .5rem; padding: 0; }
.opt { display: flex; gap: .55rem; align-items: center; font-weight: 600; margin: .4rem 0; cursor: pointer; }
.opt input { width: 1.1rem; height: 1.1rem; accent-color: var(--accent); }
.sub-box { margin: .25rem 0 .75rem 1.65rem; }
.sub-box[hidden] { display: none; }
.found { list-style: none; margin: 0; padding: 0; }
.found li { padding: .35rem 0; border-bottom: 1px solid var(--line); }
.found li:last-child { border-bottom: 0; }
.found .when { font-weight: 600; }
.found .src { display: block; font-size: .85rem; color: var(--muted); }
.found .src a { color: var(--link); }
.error { color: var(--accent); font-weight: 600; margin: 0 0 1rem; }
.error:empty { display: none; }
.actions { display: flex; flex-wrap: wrap; gap: .6rem; }
.actions button { font: 600 .95rem/1 var(--text); padding: .75rem 1.1rem; border-radius: 6px; cursor: pointer;
  border: 1.5px solid var(--line); background: transparent; color: var(--ink); }
.actions .primary { background: var(--accent); border-color: var(--accent); color: var(--accent-ink); }
.actions .danger { color: var(--accent); }
.actions button:disabled { opacity: .6; cursor: progress; }
@media (max-width: 30rem) { .show { grid-template-columns: 3.25rem 1fr; gap: .75rem; }
  .day .num { font-size: 2.2rem; } }
.chip-group { position: relative; display: inline-flex; }
.chip-group[hidden] { display: none; }
#pending-chips { display: contents; }
.chip.pending { display: inline-flex; align-items: center; gap: .35rem; }
.chip.pending .chip-x { position: static; transform: none; }
.chip-group .chip { padding-right: 2.1rem; }
.chip-x { position: absolute; right: .35rem; top: 50%; transform: translateY(-50%); min-width: 1.4rem; height: 1.4rem;
  padding: 0 .3rem; border: 0; border-radius: 999px; background: transparent; color: var(--muted);
  font: 600 .95rem/1 var(--text); cursor: pointer; }
.chip-x:hover { background: var(--accent-soft); color: var(--accent); }
.chip[aria-pressed="true"] + .chip-x { color: var(--bg); }
.chip[aria-pressed="true"] + .chip-x:hover { background: rgba(255, 255, 255, .18); color: var(--bg); }
.chip-x.confirm, .chip[aria-pressed="true"] + .chip-x.confirm { background: var(--accent); color: var(--accent-ink);
  font-size: .78rem; padding: 0 .55rem; }
.chip-group.confirming .chip { padding-right: 3.6rem; }
.chip.pending { border-style: dashed; color: var(--muted); cursor: default; }
.chip-new { border-style: dashed; color: var(--accent); border-color: var(--accent); }
.add-pill { display: inline-flex; align-items: center; gap: .25rem; padding: .2rem .25rem .2rem .9rem;
  border: 1.5px solid var(--accent); border-radius: 999px; background: var(--surface); }
.add-pill[hidden] { display: none; }
.add-pill input { width: 9rem; min-width: 0; border: 0; background: transparent; color: var(--ink);
  font: 600 .9rem/1 var(--text); padding: .35rem 0; }
.add-pill input:focus-visible { outline: none; }
.add-pill:focus-within { box-shadow: 0 0 0 3px var(--accent-soft); }
.pill-btn { height: 1.9rem; min-width: 1.9rem; border-radius: 999px; border: 0; cursor: pointer;
  font: 600 .85rem/1 var(--text); padding: 0 .7rem; }
.pill-btn.go { background: var(--accent); color: var(--accent-ink); }
.pill-btn.go:disabled { opacity: .6; cursor: progress; }
.pill-btn.help-btn { padding: 0; background: transparent; color: var(--muted); border: 1.5px solid var(--line); }
.pill-btn.help-btn[aria-expanded="true"] { color: var(--accent); border-color: var(--accent); }
.add-help { margin: .25rem 0 0; padding: .75rem 1rem; border-radius: 8px; background: var(--surface);
  color: var(--muted); font-size: .88rem; }
.add-help[hidden] { display: none; }
@media (prefers-reduced-motion: no-preference) { html { scroll-behavior: smooth; } }
"""

SCRIPT = r"""
(() => {
  const DATA = JSON.parse(document.getElementById('page-data').textContent);
  const WD = '일월화수목금토';
  let currentFilter = '';
  let removingArtists = new Set();   // 이 기기에서 뺀(아직 반영 전) 아티스트
  // 예매처 링크 주소로 예매처 이름을 붙인다
  const TICKET_SITES = [['melon.com', '멜론티켓'], ['interpark.com', '인터파크'], ['nol.com', '인터파크'],
    ['yes24.com', '예스24'], ['ticketlink.co.kr', '티켓링크'], ['ticketmaster', 'Ticketmaster'], ['axs.com', 'AXS']];
  const siteName = (url) => {
    let host = '';
    try { host = new URL(url).hostname; } catch (e) { return '예매처'; }
    const hit = TICKET_SITES.find(([d]) => host.includes(d));
    return hit ? hit[1] : host.replace(/^www\./, '');
  };
  const KEY_STORE = 'concert-watch-key', PENDING_STORE = 'concert-watch-pending';
  const ls = {
    get: (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set: (k, v) => { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch (e) {} },
  };
  const readPending = () => { try { return JSON.parse(ls.get(PENDING_STORE) || '{}') || {}; } catch (e) { return {}; } };
  // 보냈지만 아직 Actions가 반영하지 않은 변경(최대 2시간)을 화면에 먼저 보여준다
  const withPending = (base) => {
    const out = { ...(base || {}) }, pend = readPending(), now = Date.now();
    Object.entries(pend).forEach(([k, v]) => {
      if (!v || now - v.at > 2 * 3600e3) { delete pend[k]; return; }
      if (v.change.action === 'delete') out[k] = { ...(out[k] || {}), mode: 'off' }; else out[k] = v.change.pick;
    });
    ls.set(PENDING_STORE, JSON.stringify(pend));
    return out;
  };
  const activeOnly = (all) => Object.fromEntries(Object.entries(all || {}).filter(([, p]) => p && p.mode !== 'off'));
  let picks = activeOnly(withPending(DATA.picks));

  // ── 날짜 ──
  const pad = (n) => String(n).padStart(2, '0');
  const fmt = (d) => `${d.getMonth() + 1}월 ${d.getDate()}일(${WD[d.getDay()]}) ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const toLocalInput = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const left = (ms) => {
    if (ms <= 0) return '예매가 열렸어요';
    const m = Math.floor(ms / 60000), d = Math.floor(m / 1440), h = Math.floor((m % 1440) / 60), mm = m % 60;
    if (d) return `${d}일 ${h}시간 남음`;
    if (h) return `${h}시간 ${mm}분 남음`;
    return `${Math.max(mm, 1)}분 남음`;
  };
  document.querySelectorAll('.sale[data-utc]').forEach((el) => {
    const d = new Date(el.dataset.utc);
    if (!isNaN(d)) el.textContent = `일반 예매 오픈 ${fmt(d)}`;
  });

  // ── 저장된 계획은 GitHub 공개 API로 읽는다(변경은 ntfy로 보낸다) ──
  const b64decode = (s) => new TextDecoder().decode(Uint8Array.from(atob(s.replace(/\n/g, '')), (c) => c.charCodeAt(0)));
  const readPicks = async () => {
    const url = `https://api.github.com/repos/${DATA.repo}/contents/data/picks.json?ref=${encodeURIComponent(DATA.branch)}`;
    const r = await fetch(url, { cache: 'no-store', headers: { 'Accept': 'application/vnd.github+json' } });
    if (r.status === 404) return { picks: {} };
    if (!r.ok) throw new Error(`GitHub 응답 ${r.status}`);
    const j = await r.json();
    return { picks: (JSON.parse(b64decode(j.content)).picks) || {} };
  };
  // ── 비밀문구 서명 → ntfy (concert_watch/remote.py와 같은 방식) ──
  const enc = new TextEncoder();
  const hex = (buf) => Array.from(new Uint8Array(buf), (b) => b.toString(16).padStart(2, '0')).join('');
  const unhex = (h) => Uint8Array.from(h.match(/../g), (x) => parseInt(x, 16));
  const deriveKey = async (pass) => {
    const base = await crypto.subtle.importKey('raw', enc.encode(pass), 'PBKDF2', false, ['deriveBits']);
    return hex(await crypto.subtle.deriveBits({ name: 'PBKDF2', hash: 'SHA-256',
      salt: enc.encode(`concert-watch:${DATA.repo}`), iterations: 200000 }, base, 256));
  };
  const hmacHex = async (keyHex, text) => {
    const k = await crypto.subtle.importKey('raw', unhex(keyHex), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
    return hex(await crypto.subtle.sign('HMAC', k, enc.encode(text)));
  };
  const keyOk = async (k) => !!k && (await hmacHex(k, 'concert-watch-check')) === DATA.edit.check;

  // ── 화면 ──
  const status = document.getElementById('status');
  const say = (text) => { status.textContent = text; };

  // 리마인더에 쓰일 예매 오픈 일시(직접 입력이면 그 시간, 자동이면 찾은 시간들)
  const isManual = (p) => p.mode === 'manual' || (!!p.open_at && p.mode !== 'auto');
  const slots = (key, p) => (isManual(p)
    ? [{ label: '예매', at: p.open_at, source: '직접 입력' }]
    : (DATA.times[key] || []))
    .map((t) => ({ ...t, d: new Date(t.at) })).filter((t) => !isNaN(t.d)).sort((x, y) => x.d - y.d);
  const upcomingSlots = (key, p) => slots(key, p).filter((t) => t.d.getTime() > Date.now());
  const describe = (key, p) => {
    const next = upcomingSlots(key, p);
    if (!next.length) return isManual(p) ? '예매 오픈 일시가 지났어요.' : '예매 오픈 일시가 발표되면 자동으로 넣고 알려드려요.';
    const more = next.length > 1 ? ` 외 ${next.length - 1}건` : '';
    return `${next[0].label} ${fmt(next[0].d)}${more}. 하루 전과 10분 전에 알려드려요.`;
  };

  const render = () => {
    document.querySelectorAll('.pick[data-key]').forEach((btn) => {
      const p = picks[btn.dataset.key];
      btn.setAttribute('aria-pressed', String(!!p));
      btn.textContent = p ? '오픈 일시 수정' : '예매할게요';
    });
    document.querySelectorAll('.unpick[data-key]').forEach((btn) => { btn.hidden = !picks[btn.dataset.key]; });
    document.querySelectorAll('.pick-state').forEach((el) => {
      const p = picks[el.dataset.key];
      el.hidden = !p;
      if (p) el.textContent = describe(el.dataset.key, p);
    });

    const now = Date.now();
    const list = Object.entries(picks).map(([key, p]) => {
      const all = slots(key, p);
      const next = all.filter((t) => t.d.getTime() > now);
      return { key, p, all, next, waiting: !all.length && !isManual(p) };
    }).filter((x) => x.waiting || x.all.some((t) => t.d.getTime() > now - 86400000))
      .sort((x, y) => ((x.next[0] ? x.next[0].d.getTime() : Infinity) - (y.next[0] ? y.next[0].d.getTime() : Infinity)));

    const plan = document.getElementById('plan');
    const ol = plan.querySelector('ol');
    ol.replaceChildren();
    plan.hidden = !list.length;
    list.forEach(({ key, p, all, next, waiting }) => {
      const li = document.createElement('li');
      li.className = 'show';
      li.innerHTML = `<div class="day" aria-hidden="true"><span class="num"></span><span class="wd"></span></div>
        <div class="body"><p class="artist"></p><h3></h3><div class="times"></div><p class="left"></p>
        <div class="links"></div><div class="pick-row"><button type="button" class="pick" aria-pressed="true">오픈 일시 수정</button><button type="button" class="unpick">예매 안 할래요</button></div></div>`;
      const first = next[0] || all[all.length - 1];
      li.querySelector('.num').textContent = first ? first.d.getDate() : '–';
      li.querySelector('.wd').textContent = first ? `${first.d.getMonth() + 1}월` : '발표 전';
      li.querySelector('.artist').textContent = p.artist || '';
      li.querySelector('h3').textContent = p.title || '';
      const times = li.querySelector('.times');
      (all.length ? all : []).forEach((t) => {
        const line = document.createElement('p');
        line.className = 'meta';
        line.textContent = `${t.label} ${fmt(t.d)}` + (isManual(p) ? '' : `, ${t.source}에서 찾음`);
        times.append(line);
      });
      li.querySelector('.left').textContent = waiting ? '예매 오픈 일시 발표를 기다리는 중'
        : (next[0] ? `${next[0].label}까지 ${left(next[0].d.getTime() - now)}` : '예매가 열렸어요');
      if (/^https?:\/\//.test(p.link || '')) {
        const a = document.createElement('a');
        a.className = 'ticket'; a.href = p.link; a.target = '_blank'; a.rel = 'noopener'; a.textContent = siteName(p.link);
        li.querySelector('.links').append(a);
      }
      li.querySelector('.pick').addEventListener('click', () => openDialog(key));
      li.querySelector('.unpick').dataset.key = key;
      ol.append(li);
    });
  };

  // ── 예매 계획 창 ──
  const dlg = document.getElementById('pick-dialog');
  const f = {
    title: dlg.querySelector('h2'), sub: dlg.querySelector('.sub'), open: dlg.querySelector('#open-at'),
    modes: dlg.querySelectorAll('input[name="mode"]'), autoBox: dlg.querySelector('#auto-box'),
    manualBox: dlg.querySelector('#manual-box'), autoList: dlg.querySelector('#auto-list'), autoHelp: dlg.querySelector('#auto-help'),
    link: dlg.querySelector('#link'), pass: dlg.querySelector('#pass'), passField: dlg.querySelector('#pass-field'), error: dlg.querySelector('.error'), save: dlg.querySelector('#save'),
    close: dlg.querySelector('#close'),
  };
  let currentKey = null;
  const mode = () => (dlg.querySelector('input[name="mode"]:checked') || {}).value || 'auto';
  const showMode = () => { f.autoBox.hidden = mode() !== 'auto'; f.manualBox.hidden = mode() !== 'manual'; };
  f.modes.forEach((r) => r.addEventListener('change', showMode));

  const openDialog = (key) => {
    currentKey = key;
    const item = DATA.items[key] || {};
    const p = picks[key];
    f.title.textContent = (p && p.artist) || item.artist || '';
    f.sub.textContent = (p && p.title) || item.title || '';
    f.error.textContent = '';

    const found = (DATA.times[key] || []).map((t) => ({ ...t, d: new Date(t.at) }))
      .filter((t) => !isNaN(t.d) && t.d.getTime() > Date.now()).sort((x, y) => x.d - y.d);
    f.autoList.replaceChildren();
    found.forEach((t) => {
      const li = document.createElement('li');
      const when = document.createElement('span');
      when.className = 'when'; when.textContent = `${t.label} ${fmt(t.d)}`;
      const src = document.createElement('span');
      src.className = 'src';
      if (/^https?:\/\//.test(t.source_url || '')) {
        const a = document.createElement('a');
        a.href = t.source_url; a.target = '_blank'; a.rel = 'noopener'; a.textContent = `${t.source}에서 찾음`;
        src.append(a);
      } else { src.textContent = `${t.source}에서 찾음`; }
      li.append(when, src);
      f.autoList.append(li);
    });
    f.autoList.hidden = !found.length;
    f.autoHelp.textContent = found.length
      ? '예매 오픈 하루 전과 10분 전에 알려드려요.'
      : '아직 발표된 예매 오픈 일시가 없어요. 공식 인스타그램이나 사이트에 발표되면 자동으로 넣고 알려드릴게요.';

    const manual = p ? isManual(p) : false;
    f.modes.forEach((r) => { r.checked = r.value === (manual ? 'manual' : 'auto'); });
    const seed = (p && p.open_at) ? new Date(p.open_at) : (found[0] ? found[0].d : null);
    f.open.value = seed && !isNaN(seed) ? toLocalInput(seed) : '';
    showMode();

    f.link.value = (p && p.link) || item.link || '';
    f.passField.hidden = !DATA.edit.passphrase || !!ls.get(KEY_STORE);
    f.pass.value = '';
    f.save.textContent = p ? '변경 저장' : '저장';
    dlg.showModal();
    (dlg.querySelector('input[name="mode"]:checked') || f.modes[0]).focus();
  };

  const ready = () => {
    if (!DATA.repo || !DATA.edit.topic || (DATA.edit.passphrase && !DATA.edit.check)) {
      f.error.textContent = '아직 비밀문구가 설정되지 않았어요. README의 "예매 계획 저장"대로 PICK_PASSPHRASE Secret을 넣어주세요.';
      return false;
    }
    return true;
  };
  const post = async (change) => {
    const k = ls.get(KEY_STORE);
    const m = JSON.stringify({ change, ts: Math.floor(Date.now() / 1000), nonce: hex(crypto.getRandomValues(new Uint8Array(12))) });
    const body = DATA.edit.passphrase ? { m, s: await hmacHex(k, m) } : { m };
    const r = await fetch(`${DATA.edit.server}/${DATA.edit.topic}`, { method: 'POST', body: JSON.stringify(body) });
    if (!r.ok) throw new Error(`ntfy 응답 ${r.status}`);
  };
  const sendChange = async (change) => {
    let k = ls.get(KEY_STORE);
    if (DATA.edit.passphrase && !(await keyOk(k))) {
      ls.set(KEY_STORE, null);
      const pass = f.pass.value;
      if (!pass) { f.passField.hidden = false; f.pass.focus(); throw new Error('비밀문구를 넣어주세요.'); }
      k = await deriveKey(pass);
      if (!(await keyOk(k))) { f.passField.hidden = false; throw new Error('비밀문구가 맞지 않아요.'); }
      ls.set(KEY_STORE, k);
    }
    await post(change);
    const pend = readPending();
    pend[change.key] = { change, at: Date.now() };
    ls.set(PENDING_STORE, JSON.stringify(pend));
    picks = activeOnly(withPending(picks));
    render();
  };
  const submit = async (change, done) => {
    if (!ready()) return;
    const label = f.save.textContent;
    [f.save, f.close].forEach((b) => { b.disabled = true; });
    f.save.textContent = '저장하는 중…';
    try {
      await sendChange(change);
      dlg.close();
      say(`저장했어요. 몇 분 안에 반영돼요. ${done}`);
    } catch (e) { f.error.textContent = e.message; } finally {
      [f.save, f.close].forEach((b) => { b.disabled = false; });
      f.save.textContent = label;
    }
  };

  f.save.addEventListener('click', () => {
    const m = mode();
    let d = null;
    if (m === 'manual') {
      d = new Date(f.open.value);
      if (!f.open.value || isNaN(d)) { f.error.textContent = '예매 오픈 일시를 넣어주세요.'; f.open.focus(); return; }
      if (d.getTime() <= Date.now()) { f.error.textContent = '예매 오픈 일시가 이미 지났어요. 날짜와 시간을 확인해 주세요.'; return; }
    }
    const link = f.link.value.trim();
    if (link && !/^https?:\/\//.test(link)) { f.error.textContent = '예매처 링크는 http:// 또는 https://로 시작해야 해요.'; return; }
    if (!ready()) return;
    const key = currentKey, item = DATA.items[key] || {}, prev = picks[key] || {};
    const record = {
      artist: prev.artist || item.artist || '', title: prev.title || item.title || '',
      anchor: prev.anchor || item.anchor || '', country: prev.country || item.country || '',
      start: prev.start || item.start || '', mode: m, open_at: m === 'manual' ? d.toISOString() : '',
      link, picked_at: prev.picked_at || new Date().toISOString(),
    };
    submit({ action: 'set', key, pick: record }, describe(key, record));
  });

  // '예매 안 할래요': 한 번 누르면 확인으로 바뀌고, 3초 안에 한 번 더 누르면 취소
  const unpickTimers = new WeakMap();
  const resetUnpick = (b) => { b.classList.remove('confirm'); b.textContent = '예매 안 할래요'; };
  document.addEventListener('click', async (ev) => {
    const b = ev.target.closest('.unpick');
    if (!b || !b.dataset.key) return;
    if (!b.classList.contains('confirm')) {
      b.classList.add('confirm'); b.textContent = '정말 안 할래요?';
      clearTimeout(unpickTimers.get(b));
      unpickTimers.set(b, setTimeout(() => resetUnpick(b), 3000));
      return;
    }
    clearTimeout(unpickTimers.get(b));
    if (!(await canEdit())) { resetUnpick(b); return; }
    b.disabled = true;
    try {
      const change = { action: 'delete', key: b.dataset.key };
      await post(change);
      const pend = readPending();
      pend[change.key] = { change, at: Date.now() };
      ls.set(PENDING_STORE, JSON.stringify(pend));
      picks = activeOnly(withPending(picks));
      render();
      say('예매 계획을 취소했어요. 하루 안에 예정된 알림은 이미 예약돼 있어서 한 번 더 올 수 있어요.');
    } catch (e) { say(`취소하지 못했어요. ${e.message}`); } finally { b.disabled = false; resetUnpick(b); }
  });
  f.close.addEventListener('click', () => dlg.close());

  document.querySelectorAll('.pick[data-key]').forEach((btn) => btn.addEventListener('click', () => openDialog(btn.dataset.key)));

  // ── 가수 추가·빼기 (다음 정시 실행 때 반영, 그 전까지는 이 기기에서 먼저 보여줌) ──
  const ARTIST_PENDING = 'concert-watch-artists-pending';
  const addOpen = document.getElementById('add-open'), addForm = document.getElementById('add-artist');
  const nameIn = document.getElementById('artist-name'), helpBtn = document.getElementById('add-help-btn');
  const helpBox = document.getElementById('add-help'), pendingWrap = document.getElementById('pending-chips');
  const known = new Set(Array.from(document.querySelectorAll('.chip-group'), (g) => g.dataset.artist.toLowerCase()));
  const readArtistPending = () => {
    let list = [];
    try { list = JSON.parse(ls.get(ARTIST_PENDING) || '[]') || []; } catch (e) {}
    // 2시간이 지났거나 이미 반영된 것은 지운다
    list = list.filter((x) => Date.now() - x.at < 2 * 3600e3
      && (x.action === 'add' ? !known.has(x.name.toLowerCase()) : known.has(x.name.toLowerCase())));
    ls.set(ARTIST_PENDING, JSON.stringify(list));
    return list;
  };
  const showArtistPending = () => {
    const list = readArtistPending();
    pendingWrap.replaceChildren(...list.filter((x) => x.action === 'add').map((x) => {
      const c = document.createElement('span');
      c.className = 'chip pending';
      const label = document.createElement('span');
      label.textContent = `${x.name} · 추가 중`;
      const cancel = document.createElement('button');
      cancel.type = 'button'; cancel.className = 'chip-x'; cancel.textContent = '×';
      cancel.setAttribute('aria-label', `${x.name} 추가 취소`);
      cancel.addEventListener('click', async () => {
        if (!(await canEdit())) return;
        cancel.disabled = true;
        try {
          await post({ action: 'remove_artist', name: x.name });
          const rest = readArtistPending().filter((y) => y.name.toLowerCase() !== x.name.toLowerCase());
          ls.set(ARTIST_PENDING, JSON.stringify(rest));
          showArtistPending();
          say(`${x.name} 추가를 취소했어요.`);
        } catch (e) { say(`취소하지 못했어요. ${e.message}`); cancel.disabled = false; }
      });
      c.append(label, cancel);
      return c;
    }));
    removingArtists = new Set(list.filter((x) => x.action === 'remove').map((x) => x.name.toLowerCase()));
    document.querySelectorAll('.chip-group').forEach((g) => { g.hidden = removingArtists.has(g.dataset.artist.toLowerCase()); });
    // 뺀 아티스트의 공연을 숨기고, 그래서 빈 달은 달째로 숨긴다(다른 공연이 남은 달은 그대로)
    apply(removingArtists.has(currentFilter.toLowerCase()) ? '' : currentFilter);
  };
  const pushArtistPending = (action, name) => {
    const list = readArtistPending().filter((x) => x.name.toLowerCase() !== name.toLowerCase());
    list.push({ action, name, at: Date.now() });
    ls.set(ARTIST_PENDING, JSON.stringify(list));
    showArtistPending();
  };
  const canEdit = async () => {
    if (!DATA.edit.topic) { say('편집 설정(edit_topic)이 없어요.'); return false; }
    if (DATA.edit.passphrase && !(await keyOk(ls.get(KEY_STORE)))) {
      say('먼저 예매 계획 창에서 비밀문구를 한 번 넣어주세요.'); return false;
    }
    return true;
  };

  addOpen.addEventListener('click', () => {
    addOpen.hidden = true; addForm.hidden = false; addOpen.setAttribute('aria-expanded', 'true'); nameIn.focus();
  });
  const closeAdd = () => {
    addForm.hidden = true; addOpen.hidden = false; addOpen.setAttribute('aria-expanded', 'false');
    helpBox.hidden = true; helpBtn.setAttribute('aria-expanded', 'false');
  };
  nameIn.addEventListener('keydown', (ev) => { if (ev.key === 'Escape') { nameIn.value = ''; closeAdd(); addOpen.focus(); } });
  helpBtn.addEventListener('click', () => {
    helpBox.hidden = !helpBox.hidden; helpBtn.setAttribute('aria-expanded', String(!helpBox.hidden));
  });
  addForm.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const name = nameIn.value.trim().replace(/\s+/g, ' ');
    if (!name) return;
    if (known.has(name.toLowerCase()) && !readArtistPending().some((x) => x.action === 'remove' && x.name.toLowerCase() === name.toLowerCase())) {
      say(`${name}은(는) 이미 추적 중이에요.`); return;
    }
    if (!(await canEdit())) return;
    const btn = addForm.querySelector('.go');
    btn.disabled = true;
    try {
      await post({ action: 'add_artist', name });
      pushArtistPending('add', name);
      nameIn.value = ''; closeAdd();
      say(`${name}을(를) 추가했어요. 몇 분 안에 공연을 찾아 넣고, 예매 오픈 시각이 나온 국내 공연은 자동으로 알려드려요.`);
    } catch (e) { say(`추가하지 못했어요. ${e.message}`); } finally { btn.disabled = false; }
  });

  // × 한 번 누르면 '빼기'로 바뀌고, 한 번 더 누르면 뺀다(3초 안에)
  document.querySelectorAll('.chip-group').forEach((g) => {
    const x = g.querySelector('.chip-x'), name = g.dataset.artist;
    let timer = null;
    const reset = () => { x.classList.remove('confirm'); g.classList.remove('confirming'); x.textContent = '×'; x.setAttribute('aria-label', `${name} 빼기`); };
    x.addEventListener('click', async () => {
      if (!x.classList.contains('confirm')) {
        x.classList.add('confirm'); g.classList.add('confirming'); x.textContent = '빼기';
        x.setAttribute('aria-label', `${name}을(를) 정말 빼요`);
        clearTimeout(timer); timer = setTimeout(reset, 3000);
        return;
      }
      clearTimeout(timer);
      if (!(await canEdit())) { reset(); return; }
      x.disabled = true;
      try {
        await post({ action: 'remove_artist', name });
        if (g.querySelector('.chip').getAttribute('aria-pressed') === 'true') apply('');
        pushArtistPending('remove', name);
        say(`${name}을(를) 뺐어요. 몇 분 안에 반영돼요.`);
      } catch (e) { say(`빼지 못했어요. ${e.message}`); reset(); } finally { x.disabled = false; }
    });
  });

  // ── 아티스트 필터 ──
  const chips = document.querySelectorAll('.chip[data-filter]');
  function apply(name) {
    currentFilter = name;
    chips.forEach((c) => c.setAttribute('aria-pressed', String(c.dataset.filter === name)));
    document.querySelectorAll('.month:not(.plan) .show').forEach((s) => {
      const a = s.dataset.artist || '';
      s.hidden = removingArtists.has(a.toLowerCase()) || (!!name && a !== name);
    });
    document.querySelectorAll('.month:not(.plan)').forEach((m) => { m.hidden = !m.querySelector('.show:not([hidden])'); });
  }
  chips.forEach((c) => c.addEventListener('click', () => apply(c.dataset.filter)));
  showArtistPending();

  // ── 알림 링크(#id)로 들어오면 그 공연을 보여줌 ──
  const focusHash = () => {
    const el = location.hash && document.getElementById(location.hash.slice(1));
    document.querySelectorAll('.show.focus').forEach((x) => x.classList.remove('focus'));
    if (el) { el.classList.add('focus'); el.scrollIntoView({ block: 'center' }); }
  };
  window.addEventListener('hashchange', focusHash);

  render();
  focusHash();
  setInterval(render, 60000);
  if (DATA.repo) readPicks().then((r) => { picks = activeOnly(withPending(r.picks)); render(); }).catch(() => {});
  const forget = document.getElementById('forget');
  forget.hidden = !ls.get(KEY_STORE);
  forget.addEventListener('click', () => { ls.set(KEY_STORE, null); forget.hidden = true; say('이 기기에서 비밀문구를 지웠어요.'); });
})();
"""


def render_html(state: dict, cfg: dict, now: dt.datetime, picks: dict | None = None) -> str:
    title = escape((cfg.get("site") or {}).get("title") or "콘서트 알리미")
    artists = [a["name"] for a in cfg.get("artists", [])]
    tracked = set(artists)
    # 뺀 아티스트의 공연·소식은 보여주지 않는다
    shows = [e for e in upcoming(list(state.get("events", {}).values()), now.date()) if e.get("artist") in tracked]
    news = [n for n in recent_news(state, now) if n.get("artist") in tracked]
    counts = Counter(e["artist"] for e in shows)

    months: dict[tuple[int, int], list[str]] = {}
    for e in shows:
        months.setdefault((e["_shown"].year, e["_shown"].month), []).append(_row(e, now))
    if months:
        body = "".join(
            f'<section class="month"><h2>{y}년 {m}월</h2><ol>{"".join(rows)}</ol></section>'
            for (y, m), rows in months.items()
        )
    else:
        body = ('<p class="empty">아직 예정된 공연이 없어요. 새 공연이 뜨면 여기에 나타나고 알림도 보내드려요. '
                '추적할 아티스트는 config.yaml에서 바꿀 수 있어요.</p>')
    news_html = (f'<section class="month"><h2>최근 공식 소식</h2><ol>{"".join(_news_item(n) for n in news)}</ol></section>'
                 if news else "")

    chips = [f'<button type="button" class="chip" aria-pressed="true" data-filter="">전체 {len(shows)}</button>']
    chips += [
        f'<span class="chip-group" data-artist="{escape(a)}">'
        f'<button type="button" class="chip" aria-pressed="false" data-filter="{escape(a)}">{escape(a)} {counts.get(a, 0)}</button>'
        f'<button type="button" class="chip-x" aria-label="{escape(a)} 빼기" title="빼기">×</button></span>'
        for a in artists
    ]

    checked_txt = ""
    if state.get("last_checked"):
        c = dt.datetime.fromisoformat(state["last_checked"])
        checked_txt = f"마지막 확인 {c.month}월 {c.day}일 {c.hour:02d}:{c.minute:02d}"
    errors = state.get("last_errors") or []
    error_html = (f'<p class="warn">최근 확인에서 일부 소스를 불러오지 못했어요: {escape("; ".join(errors))}</p>'
                  if errors else "")

    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{title}</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🎫</text></svg>">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Black+Han+Sans&family=IBM+Plex+Sans+KR:wght@400;600&display=swap" rel="stylesheet">
<style>{STYLE}</style>
</head>
<body>
<main class="wrap">
  <header>
    <h1>{title}</h1>
    <p>{escape(checked_txt)}</p>
    {error_html}
    <p class="status" id="status" role="status" aria-live="polite"></p>
  </header>
  <section class="month plan" id="plan" hidden><h2>내 예매 계획</h2><ol></ol></section>
  <nav class="filters" aria-label="아티스트별로 보기">{"".join(chips)}<span id="pending-chips"></span>
    <button type="button" class="chip chip-new" id="add-open" aria-expanded="false">+ 가수 추가</button>
    <form class="add-pill" id="add-artist" autocomplete="off" hidden>
      <input id="artist-name" type="text" maxlength="40" placeholder="가수 이름" aria-label="추가할 가수 이름" required>
      <button type="submit" class="pill-btn go">추가</button>
      <button type="button" class="pill-btn help-btn" id="add-help-btn" aria-label="어디서 찾나요?" aria-expanded="false" aria-controls="add-help">?</button>
    </form>
  </nav>
  <p class="add-help" id="add-help" hidden>국내 공연 DB(KOPIS), 멜론티켓·인터파크·예스24 티켓오픈 공지, Ticketmaster, 뉴스에서 공연을 찾아요. 추가·삭제는 몇 분 안에 반영돼요.</p>
  {news_html}
  {body}
  <footer>
    <p>공연 일정은 KOPIS 공연예술통합전산망(국내)과 Ticketmaster(해외), 공식 소식은 아티스트의 공식 인스타그램과 웹사이트에서 가져와요. 예매 전에 공식 예매처에서 다시 확인하세요.</p>
    <p><button type="button" id="forget" hidden>이 기기에서 비밀문구 지우기</button></p>
  </footer>
</main>

<dialog id="pick-dialog" aria-labelledby="pick-title">
  <h2 id="pick-title"></h2>
  <p class="sub"></p>
  <fieldset class="field">
    <legend>예매 오픈 일시</legend>
    <label class="opt"><input type="radio" name="mode" value="auto"> 공식 발표에서 자동으로 찾기</label>
    <div class="sub-box" id="auto-box">
      <ul class="found" id="auto-list"></ul>
      <p class="help" id="auto-help"></p>
    </div>
    <label class="opt"><input type="radio" name="mode" value="manual"> 직접 입력</label>
    <div class="sub-box" id="manual-box">
      <input id="open-at" type="datetime-local" aria-label="예매 오픈 일시 직접 입력">
      <p class="help">공지에서 확인한 시간을 넣어주세요. 선예매를 할 거라면 선예매 시간을 넣으세요.</p>
    </div>
  </fieldset>
  <div class="field">
    <label for="link">예매처 링크</label>
    <input id="link" type="url" inputmode="url" placeholder="https://">
    <p class="help">알림을 누르면 이 링크로 바로 가요. 비워둬도 돼요.</p>
  </div>
  <div class="field" id="pass-field" hidden>
    <label for="pass">비밀문구</label>
    <input id="pass" type="password" autocomplete="current-password" spellcheck="false">
    <p class="help">GitHub Secret PICK_PASSPHRASE에 넣은 문구예요. 이 기기에서 처음 한 번만 넣으면 돼요.</p>
  </div>
  <p class="error" role="alert"></p>
  <div class="actions">
    <button type="button" class="primary" id="save">저장</button>
    <button type="button" id="close">닫기</button>
  </div>
</dialog>

<script type="application/json" id="page-data">{_page_data(shows, news, picks or {}, cfg, state)}</script>
<script>{SCRIPT}</script>
</body>
</html>
"""


def render(state: dict, cfg: dict, now: dt.datetime, out_dir: Path, picks: dict | None = None) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "index.html").write_text(render_html(state, cfg, now, picks), encoding="utf-8")
    shows = [{k: v for k, v in e.items() if not k.startswith("_")}
             for e in upcoming(list(state.get("events", {}).values()), now.date())]
    (out_dir / "events.json").write_text(
        json.dumps({"updated": state.get("last_checked"), "events": shows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
