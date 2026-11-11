import datetime as dt
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import watch  # noqa: E402
import concert_watch.sources as _src  # noqa: E402
_src.KOPIS_PAUSE = 0  # 테스트에서는 쉬지 않음
from concert_watch import notify, official, site, sources  # noqa: E402

NOW = dt.datetime(2026, 11, 11, 14, 20, tzinfo=ZoneInfo("Asia/Seoul"))
CFG = {
    "artists": [{"name": "아이유", "exclude_keywords": ["트리뷰트"]}, {"name": "Coldplay"}],
    "notify": {"email": True, "sms": True, "push": True},
    "site": {"title": "콘서트 알리미", "url": "https://example.github.io/concert-watch/"},
}

KOPIS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<dbs>
  <db><mt20id>PF100001</mt20id><prfnm>2026 아이유 콘서트 [서울]</prfnm>
      <prfpdfrom>2026.12.24</prfpdfrom><prfpdto>2026.12.26</prfpdto>
      <fcltynm>KSPO DOME</fcltynm><area>서울특별시</area><genrenm>대중음악</genrenm></db>
  <db><mt20id>PF100002</mt20id><prfnm>아이유 트리뷰트 콘서트</prfnm>
      <prfpdfrom>2026.12.01</prfpdfrom><prfpdto>2026.12.01</prfpdto>
      <fcltynm>소극장</fcltynm><area>서울특별시</area></db>
</dbs>""".encode()

TM_EVENT = {
    "id": "G5abc", "name": "Coldplay: Music Of The Spheres", "url": "https://www.ticketmaster.com/event/G5abc",
    "dates": {"start": {"localDate": "2027-01-15", "localTime": "19:30:00"}},
    "sales": {"public": {"startDateTime": "2026-11-20T15:00:00Z"}},
    "_embedded": {"venues": [{"name": "Wembley Stadium", "city": {"name": "London"},
                              "country": {"countryCode": "GB"}}]},
}


def ev(key, artist, start, end=None):
    return {"key": key, "source": "test", "artist": artist, "title": f"{artist} 공연", "start": start,
            "end": end or start, "venue": "공연장", "city": "서울", "ticket_links": [], "url": ""}


def test_parse_kopis_excludes_and_normalizes_dates():
    events = sources.parse_kopis_list(KOPIS_XML, CFG["artists"][0])
    assert [e["key"] for e in events] == ["kopis:PF100001"]
    assert events[0]["start"] == "2026-12-24" and events[0]["end"] == "2026-12-26"


def test_parse_ticketmaster():
    e = sources.parse_tm_event(TM_EVENT, {"name": "Coldplay"})
    assert e["time"] == "19:30" and e["country"] == "GB" and e["ticket_links"][0]["name"] == "Ticketmaster"


def test_first_run_is_baseline_then_new_events_notify(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "dispatch", lambda events, cfg, dry_run=False: sent.append(events))
    state = {"events": {}}

    first = watch.run(CFG, state, NOW, [ev("a", "아이유", "2026-12-24")], [])
    assert first == [] and sent == []          # 기존 공연은 알림 없이 기준선
    assert "a" in state["events"]

    second = watch.run(CFG, state, NOW, [ev("a", "아이유", "2026-12-24"), ev("b", "Coldplay", "2027-01-15")], [])
    assert [e["key"] for e in second] == ["b"]  # 새로 뜬 공연만
    assert len(sent) == 1

    third = watch.run(CFG, state, NOW, [ev("a", "아이유", "2026-12-24"), ev("b", "Coldplay", "2027-01-15")], [])
    assert third == []                          # 같은 공연은 다시 알리지 않음


def test_source_error_keeps_baseline_open(monkeypatch):
    monkeypatch.setattr(notify, "dispatch", lambda *a, **k: None)
    state = {"events": {}}
    watch.run(CFG, state, NOW, [], ["KOPIS(HTTPError)"])
    assert not state.get("artists_seen")         # 실패한 회차는 기준선 확정 안 함
    assert state["last_api_errors"] == ["KOPIS(HTTPError)"]


def test_skipped_api_round_does_not_close_baseline(monkeypatch):
    monkeypatch.setattr(notify, "dispatch", lambda *a, **k: None)
    state = {"events": {}}
    watch.run(CFG, state, NOW, None, [])          # 공연 API를 건너뛴 회차
    assert not state.get("artists_seen") and "last_api_check" not in state
    assert watch.api_due({"api_interval_hours": 3}, state, NOW)
    state["last_api_check"] = (NOW - dt.timedelta(hours=1)).isoformat()
    assert not watch.api_due({"api_interval_hours": 3}, state, NOW)
    state["last_api_check"] = (NOW - dt.timedelta(hours=2, minutes=55)).isoformat()
    assert watch.api_due({"api_interval_hours": 3}, state, NOW)


def test_prune_drops_long_finished_events():
    state = {"events": {"old": ev("old", "아이유", "2026-09-01"), "soon": ev("soon", "아이유", "2026-12-01")}}
    watch.prune(state, NOW.date())
    assert list(state["events"]) == ["soon"]


def test_messages():
    events = [ev("a", "아이유", "2026-12-24", "2026-12-26")]
    text = notify.build_text(events, CFG["site"]["url"])
    assert "2026.12.24(목)~2026.12.26(토)" in text and CFG["site"]["url"] in text
    sms = notify.build_sms(events * 5, "")
    assert "외 2건" in sms


def test_dry_run_sends_nothing(caplog):
    caplog.set_level("INFO")
    results = notify.dispatch([ev("a", "아이유", "2026-12-24")], CFG, dry_run=True)
    assert results == {"email": "dry-run", "sms": "dry-run", "push": "dry-run"}


def test_missing_secret_is_skipped(monkeypatch):
    for k in ["SMTP_USER", "SMTP_PASSWORD", "NOTIFY_EMAIL_TO"]:
        monkeypatch.delenv(k, raising=False)
    results = notify.dispatch([ev("a", "아이유", "2026-12-24")], {"notify": {"email": True}})
    assert results == {"email": "skipped"}


def test_site_renders_and_escapes(tmp_path):
    bad = ev("x", "아이유", "2026-12-24")
    bad["title"] = "<script>alert(1)</script>"
    bad["ticket_links"] = [{"name": "인터파크", "url": "https://tickets.interpark.com/goods/1"},
                           {"name": "위험", "url": "javascript:alert(1)"}]
    bad["first_seen"] = NOW.isoformat()
    state = {"events": {"x": bad, "past": ev("past", "아이유", "2026-10-01")}, "last_checked": NOW.isoformat()}
    site.render(state, CFG, NOW, tmp_path)
    html = (tmp_path / "index.html").read_text()
    assert "<script>alert(1)</script>" not in html
    assert "javascript:alert" not in html
    assert "2026년 12월" in html and "새로 떴어요" in html and "인터파크" in html
    assert "2026년 10월" not in html


# ── 공식 채널 ──────────────────────────────────────────
OFF_CFG = {"artists": [{"name": "Coldplay", "instagram": "coldplay",
                        "official_pages": ["https://www.coldplay.com/tour/"]}]}
PAGE_V1 = """<html><head><title>Tour</title><script>var x=1;</script></head><body>
<nav><a>Home</a><a>Music</a><a>Store</a></nav>
<div class="tour"><div>Music Of The Spheres</div><div>Sep 08 2025 London Wembley Stadium</div></div>
<div>Hoodie $29.99</div><footer>Sign up for the newsletter</footer></body></html>"""
PAGE_V2 = PAGE_V1.replace("</div></div>", "</div><div>Mar 14 2027 Seoul Goyang Stadium</div></div>") \
                 .replace("Hoodie $29.99", "T-shirt $35.99")


def test_page_lines_strip_scripts():
    lines = official.page_lines(PAGE_V1)
    assert "var x=1;" not in " ".join(lines) and "Sep 08 2025 London Wembley Stadium" in lines


def test_official_page_baseline_then_new_dates(monkeypatch):
    pages = {"v": PAGE_V1}
    monkeypatch.setattr(official, "fetch_page_lines", lambda url: official.page_lines(pages["v"]))
    monkeypatch.delenv("IG_USER_ID", raising=False)
    state = {}
    news, errors = official.check(OFF_CFG, state, NOW)
    assert news == [] and errors == []            # 첫 확인은 기준선
    pages["v"] = PAGE_V2
    news, _ = official.check(OFF_CFG, state, NOW)
    assert len(news) == 1 and "Seoul" in news[0]["summary"]
    assert "T-shirt" not in news[0]["summary"]    # 날짜·공연 단어가 없는 변경은 무시
    news, _ = official.check(OFF_CFG, state, NOW)
    assert news == []                              # 같은 내용은 다시 알리지 않음


def test_instagram_only_concert_posts(monkeypatch):
    monkeypatch.setenv("IG_USER_ID", "1789")
    monkeypatch.setenv("IG_ACCESS_TOKEN", "tok")
    posts = {"v": [{"id": "1", "caption": "New single out now", "permalink": "https://instagram.com/p/1",
                    "timestamp": "2026-11-01T10:00:00+0000"}]}
    monkeypatch.setattr(official, "fetch_instagram", lambda *a, **k: posts["v"])
    monkeypatch.setattr(official, "fetch_page_lines", lambda url: ["a" * 5] * 6)
    state = {}
    assert official.check(OFF_CFG, state, NOW)[0] == []
    posts["v"] = [
        {"id": "3", "caption": "SEOUL! Tickets on sale Friday 10am KST", "permalink": "https://instagram.com/p/3",
         "timestamp": "2026-11-11T01:00:00+0000"},
        {"id": "2", "caption": "Studio day", "permalink": "https://instagram.com/p/2",
         "timestamp": "2026-11-10T01:00:00+0000"},
    ] + posts["v"]
    news, _ = official.check(OFF_CFG, state, NOW)
    assert [n["key"] for n in news] == ["ig:3"]
    assert official.check(OFF_CFG, state, NOW)[0] == []


def test_news_flows_to_notify_and_site(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(notify, "dispatch", lambda items, cfg, dry_run=False: sent.append(items))
    item = {"key": "ig:3", "kind": "announcement", "source": "instagram", "artist": "Coldplay",
            "title": "SEOUL! Tickets on sale Friday", "summary": "SEOUL! Tickets on sale Friday 10am KST",
            "url": "https://instagram.com/p/3", "first_seen": NOW.isoformat()}
    state = {"events": {}}
    watch.run(CFG, state, NOW, None, [], news=[item])
    watch.run(CFG, state, NOW, None, [], news=[item])
    assert len(sent) == 1                          # 같은 소식은 한 번만
    text = notify.build_text(sent[0], "")
    assert "공식 소식 1건" in text and "공식 인스타그램" in text
    assert "공식 인스타그램" in notify.build_sms(sent[0], "")
    site.render(state, CFG, NOW, tmp_path)
    html = (tmp_path / "index.html").read_text()
    assert "최근 공식 소식" in html and "SEOUL! Tickets on sale Friday" in html


# ── 예매 리마인더 ──────────────────────────────────────
from concert_watch import reminders  # noqa: E402

RCFG = {"notify": {"push": True, "email": True, "sms": False}}


def _capture(monkeypatch):
    pushes, mails = [], []
    monkeypatch.setattr(notify, "push_raw",
                        lambda title, msg, click="", at=None, priority=None: pushes.append((title, at, priority)))
    monkeypatch.setattr(notify, "send_mail_raw", lambda subject, text, html=None: mails.append(subject))
    return pushes, mails


def _pick(open_at):
    return {"artist": "Coldplay", "title": "Music Of The Spheres Seoul", "link": "https://tickets.example/1",
            "open_at": open_at.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")}


def test_reminders_schedule_exact_times_only_inside_window(monkeypatch):
    pushes, mails = _capture(monkeypatch)
    open_at = NOW + dt.timedelta(days=5)
    picks, state = {"k": _pick(open_at)}, {}

    reminders.process(RCFG, picks, state, NOW)
    assert pushes == [] and mails == []                       # 아직 3시간 창 밖

    t1 = open_at - dt.timedelta(days=1, hours=2)               # 하루 전 알림 2시간 전
    reminders.process(RCFG, picks, state, t1)
    assert len(pushes) == 1
    title, at, prio = pushes[0]
    assert at == open_at - dt.timedelta(days=1) and "내일 14:20" in title and prio == 4
    assert mails == []                                         # 이메일은 시각이 되면

    t2 = open_at - dt.timedelta(days=1) + dt.timedelta(minutes=20)   # 크론이 20분 늦게 돌아도
    reminders.process(RCFG, picks, state, t2)
    assert len(pushes) == 1 and len(mails) == 1               # 푸시는 중복 없이, 이메일만 발송

    t3 = open_at - dt.timedelta(hours=2, minutes=30)
    reminders.process(RCFG, picks, state, t3)
    assert len(pushes) == 2
    title, at, prio = pushes[1]
    assert at == open_at - dt.timedelta(minutes=10) and "10분 뒤" in title and prio == 5

    reminders.process(RCFG, picks, state, open_at - dt.timedelta(minutes=5))
    assert len(pushes) == 2 and len(mails) == 1               # 더 보내지 않음


def test_late_pick_sends_now_and_schedules_10min(monkeypatch):
    pushes, mails = _capture(monkeypatch)
    open_at = NOW + dt.timedelta(hours=2)                      # 오픈 2시간 전에 고름
    reminders.process(RCFG, {"k": _pick(open_at)}, {}, NOW)
    kinds = sorted((at is None, t) for t, at, _ in pushes)
    assert len(pushes) == 2
    assert any(at is None and "2시간 뒤 열려요" in t for t, at, _ in pushes)       # 하루 전 대신 지금
    assert any(at == open_at - dt.timedelta(minutes=10) for _, at, _ in pushes)  # 10분 전 예약
    assert len(mails) == 1


def test_very_late_pick_only_10min(monkeypatch):
    pushes, mails = _capture(monkeypatch)
    open_at = NOW + dt.timedelta(minutes=12)
    reminders.process(RCFG, {"k": _pick(open_at)}, {}, NOW)
    assert len(pushes) == 1 and pushes[0][1] == open_at - dt.timedelta(minutes=10) and mails == []


def test_changed_open_time_gets_new_reminders(monkeypatch):
    pushes, _ = _capture(monkeypatch)
    state = {}
    reminders.process(RCFG, {"k": _pick(NOW + dt.timedelta(hours=2))}, state, NOW)
    n = len(pushes)
    reminders.process(RCFG, {"k": _pick(NOW + dt.timedelta(hours=2, minutes=30))}, state, NOW)
    assert len(pushes) > n


def test_cancelled_or_past_picks_do_nothing(monkeypatch):
    pushes, mails = _capture(monkeypatch)
    reminders.process(RCFG, {"k": _pick(NOW - dt.timedelta(hours=1))}, {}, NOW)
    reminders.process(RCFG, {"k": {"artist": "x", "open_at": "엉터리"}}, {}, NOW)
    assert pushes == [] and mails == []


def test_notification_links_to_pick(monkeypatch):
    e = ev("kopis:PF1", "아이유", "2026-12-24")
    text = notify.build_text([e], "https://me.github.io/concert-watch/")
    assert f"https://me.github.io/concert-watch/#{notify.anchor_id('kopis:PF1')}" in text


def test_site_has_pick_ui_and_safe_data(tmp_path):
    e = ev("kopis:PF1", "아이유", "2026-12-24")
    e["title"] = "</script><script>alert(1)</script>"
    state = {"events": {"kopis:PF1": e}, "last_checked": NOW.isoformat()}
    picks = {"kopis:PF1": {"artist": "아이유", "title": "x", "open_at": "2026-12-01T11:00:00.000Z", "link": ""}}
    site.render(state, CFG, NOW, tmp_path, picks=picks)
    html = (tmp_path / "index.html").read_text()
    assert f'id="{notify.anchor_id("kopis:PF1")}"' in html and 'class="pick"' in html
    assert "</script><script>alert(1)" not in html
    assert '"open_at": "2026-12-01T11:00:00.000Z"' in html


# ── 예매 오픈 일시 자동 찾기 ───────────────────────────────
from concert_watch import opentimes  # noqa: E402


class _FakeResp:
    def __init__(self, payload, status=200):
        self.payload, self.status_code, self.text = payload, status, json.dumps(payload)

    def json(self):
        return self.payload


import json  # noqa: E402


def test_tm_sale_times_include_presales():
    ev = dict(TM_EVENT, sales={"public": {"startDateTime": "2026-11-20T10:00:00Z"},
                               "presales": [{"name": "Artist Presale", "startDateTime": "2026-11-18T10:00:00Z"}]})
    times = sources.parse_tm_event(ev, {"name": "Coldplay"})["sale_times"]
    assert [t["label"] for t in times] == ["선예매(Artist Presale)", "일반 예매"]


def test_openai_extraction_parses_reply_and_timezones(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    sent = {}
    reply = {"open_times": [
        {"label": "선예매", "date": "2026-11-18", "time": "10:00", "timezone": "Europe/London",
         "country": "gb", "city": "London", "evidence": "presale Wed 10am"},
        {"label": "일반 예매", "date": "2026-11-20", "time": "20:00", "timezone": "Asia/Seoul",
         "country": "KR", "city": "Seoul", "evidence": "일반 예매 11/20 8PM"},
        {"label": "예매", "date": "언젠가", "time": "?", "timezone": "x"},
    ]}

    def fake_post(url, **kw):
        sent.update(kw["json"], headers=kw["headers"])
        return _FakeResp({"choices": [{"message": {"content": "```json\n" + json.dumps(reply) + "\n```"}}]})

    monkeypatch.setattr(opentimes.requests, "post", fake_post)
    out = opentimes.extract("Presale Wed 10am London ...", "Coldplay", NOW, {"open_time": {"model": "gpt-4o-mini"}})
    assert sent["model"] == "gpt-4o-mini" and sent["headers"]["Authorization"] == "Bearer sk-test"
    assert [(c["label"], c["at"][:16], c["country"]) for c in out] == [
        ("선예매", "2026-11-18T10:00", "GB"),          # 런던 10시 = UTC 10시(11월)
        ("일반 예매", "2026-11-20T11:00", "KR"),       # 서울 20시 = UTC 11시
    ]


def test_openai_failure_falls_back_to_rules(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(opentimes.requests, "post", lambda *a, **k: _FakeResp({"error": "overloaded"}, 529))
    out = opentimes.extract("[서울 내한] 일반 예매: 11월 20일(금) 오후 8시", "Coldplay", NOW, {})
    assert out[0]["method"] == "rules" and out[0]["at"].startswith("2026-11-20T11:00")


def _state_with_news():
    return {
        "events": {
            "kopis:PF1": dict(ev("kopis:PF1", "Coldplay", "2027-04-20"), country="KR"),
            "tm:L1": dict(ev("tm:L1", "Coldplay", "2027-06-01"), country="GB", source="ticketmaster",
                          sale_times=[{"label": "일반 예매", "at": "2026-11-25T10:00:00+00:00"}]),
        },
        "announcements": {
            "ig:9": {"key": "ig:9", "artist": "Coldplay", "source": "instagram", "url": "https://instagram.com/p/9",
                     "first_seen": NOW.isoformat(), "open_times": [
                         {"label": "선예매", "at": "2026-11-18T10:00:00+00:00", "country": "GB"},
                         {"label": "일반 예매", "at": "2026-11-20T11:00:00+00:00", "country": "KR"},
                         {"label": "예매", "at": "2027-05-01T11:00:00+00:00", "country": None},  # 공연 뒤
                     ]},
            "ig:8": {"key": "ig:8", "artist": "Oasis", "source": "instagram", "first_seen": NOW.isoformat(),
                     "open_times": [{"label": "예매", "at": "2026-11-21T11:00:00+00:00", "country": "KR"}]},
        },
    }


def test_candidates_match_artist_country_and_date():
    state = _state_with_news()
    kr = opentimes.candidates_for("kopis:PF1", {}, state)
    assert [(c["label"], c["source"]) for c in kr] == [("일반 예매", "공식 인스타그램")]
    gb = opentimes.candidates_for("tm:L1", {}, state)
    assert [(c["label"], c["source"]) for c in gb] == [
        ("선예매", "공식 인스타그램"), ("일반 예매", "Ticketmaster"),
        ("예매", "공식 인스타그램"),                          # 나라 정보가 없는 시간은 공연 전이면 함께 씀
    ]
    own = opentimes.candidates_for("ig:9", {}, state)
    assert len(own) == 3                                   # 소식 자체를 고르면 그 글의 시간 전부


def test_effective_times_manual_vs_auto():
    state = _state_with_news()
    assert opentimes.effective_times("kopis:PF1", {"mode": "manual", "open_at": "2026-12-01T11:00:00Z"}, state)[0]["source"] == "직접 입력"
    assert opentimes.effective_times("kopis:PF1", {"open_at": "2026-12-01T11:00:00Z"}, state)[0]["source"] == "직접 입력"  # 예전 형식
    assert opentimes.effective_times("kopis:PF1", {"mode": "auto", "artist": "Coldplay"}, state)[0]["label"] == "일반 예매"


def test_auto_pick_reminds_every_found_time(monkeypatch):
    pushes, _ = _capture(monkeypatch)
    monkeypatch.setattr(notify, "broadcast", lambda *a, **k: [])
    state = _state_with_news()
    pick = {"artist": "Coldplay", "title": "UK", "mode": "auto", "country": "GB", "start": "2027-06-01",
            "picked_at": NOW.isoformat()}
    for open_utc in ("2026-11-18T10:00:00+00:00", "2026-11-25T10:00:00+00:00"):
        t = dt.datetime.fromisoformat(open_utc) - dt.timedelta(days=1, hours=1)   # 각 하루 전 알림 1시간 전
        reminders.process(RCFG, {"tm:L1": pick}, state, t.astimezone(NOW.tzinfo))
    titles = [p[0] for p in pushes]
    assert any("선예매 내일" in x for x in titles) and any("일반 예매 내일" in x for x in titles)


def test_found_notice_only_for_times_found_after_pick(monkeypatch):
    calls = []
    monkeypatch.setattr(notify, "broadcast", lambda title, text, click, cfg, dry_run=False: calls.append((title, text)))
    state = _state_with_news()
    early_pick = {"artist": "Coldplay", "title": "Seoul", "mode": "auto", "country": "KR", "start": "2027-04-20",
                  "picked_at": (NOW + dt.timedelta(minutes=5)).isoformat(), "anchor": "e-1"}
    reminders.announce_found(RCFG, {"kopis:PF1": early_pick}, state, NOW)
    assert calls == []                                     # 고를 때 이미 보였던 시간
    late_pick = dict(early_pick, picked_at=(NOW - dt.timedelta(days=3)).isoformat())
    state = _state_with_news()
    reminders.announce_found(RCFG, {"kopis:PF1": late_pick}, state, NOW)
    reminders.announce_found(RCFG, {"kopis:PF1": late_pick}, state, NOW)
    assert len(calls) == 1 and "예매 오픈 일시를 찾았어요" in calls[0][0] and "공식 인스타그램" in calls[0][1]


def test_new_news_gets_open_times(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(notify, "dispatch", lambda *a, **k: None)
    item = {"key": "web:1", "kind": "announcement", "source": "web", "artist": "실리카겔", "title": "t",
            "summary": "s", "text": "단독 콘서트 서울\n티켓 오픈: 11월 20일(금) 오후 8시 멜론티켓", "url": "",
            "published": NOW.isoformat(), "first_seen": NOW.isoformat()}
    state = {"events": {}}
    watch.run(CFG, state, NOW, None, [], news=[item])
    assert state["announcements"]["web:1"]["open_times"][0]["at"].startswith("2026-11-20T11:00")
    assert "찾은 예매 오픈 일시" in notify.build_text([state["announcements"]["web:1"]], "")


# ── 예매 계획 이슈 → data/picks.json ─────────────────────────
from concert_watch import picks as picks_store  # noqa: E402


def _issue_body(change):
    return f"설명\n\n{picks_store.MARKER}\n```json\n{json.dumps(change, ensure_ascii=False)}\n```\n"


def _event(tmp_path, body, author="me", owner="me"):
    path = tmp_path / "event.json"
    path.write_text(json.dumps({"issue": {"number": 1, "body": body, "user": {"login": author}},
                                "repository": {"owner": {"login": owner}}}), encoding="utf-8")
    return path


def test_issue_sets_and_deletes_pick(tmp_path):
    picks_path = tmp_path / "picks.json"
    pick = {"artist": "혁오", "title": "HYUKOH [33] TOUR", "mode": "manual", "open_at": "2026-11-12T11:00:00.000Z",
            "link": "javascript:alert(1)", "picked_at": "2026-11-12T00:00:00Z", "extra": "버려짐"}
    msg = watch.apply_issue(_event(tmp_path, _issue_body({"action": "set", "key": "kopis:PF1", "pick": pick})), picks_path)
    saved = picks_store.load(picks_path)["kopis:PF1"]
    assert "혁오" in msg and saved["mode"] == "manual" and saved["link"] == "" and "extra" not in saved

    watch.apply_issue(_event(tmp_path, _issue_body({"action": "delete", "key": "kopis:PF1"})), picks_path)
    assert picks_store.load(picks_path) == {}


def test_issue_from_other_user_or_bad_body_is_rejected(tmp_path):
    picks_path = tmp_path / "picks.json"
    good = _issue_body({"action": "set", "key": "k", "pick": {"mode": "auto"}})
    for body, author in [(good, "stranger"), ("그냥 이슈", "me"),
                         (_issue_body({"action": "set", "key": "k", "pick": {"mode": "manual", "open_at": "곧"}}), "me")]:
        try:
            watch.apply_issue(_event(tmp_path, body, author=author), picks_path)
        except ValueError:
            continue
        raise AssertionError(f"반영되면 안 돼요: {author} {body[:20]}")
    assert not picks_path.exists()


def test_news_search_baseline_then_only_fresh_announcements(monkeypatch):
    cfg = {"artists": [{"name": "실리카겔", "kopis_keywords": ["Silica Gel"], "news_queries": ["실리카겔"]}]}
    old = NOW - dt.timedelta(days=30)
    feed = [{"id": "a", "title": "실리카겔, 첫 아시아 투어 개최", "link": "https://n/a", "published": old}]
    monkeypatch.setattr(official, "fetch_news", lambda q: list(feed))
    state = {}
    assert official.check(cfg, state, NOW)[0] == []                  # 첫 확인은 기준만 저장
    feed += [
        {"id": "b", "title": "실리카겔 단독 콘서트 티켓 오픈 11월 20일 오후 8시", "link": "https://n/b", "published": NOW},
        {"id": "c", "title": "실리카겔, KSPO DOME 공연 성료", "link": "https://n/c", "published": NOW},
        {"id": "d", "title": "실리카겔 정규 3집 발매", "link": "https://n/d", "published": NOW},
        {"id": "e", "title": "실리카겔 내한 투어 예매", "link": "https://n/e", "published": old},
    ]
    news, errors = official.check(cfg, state, NOW)
    assert errors == [] and len(news) == 1
    assert news[0]["source"] == "news" and news[0]["url"] == "https://n/b" and "티켓 오픈" in news[0]["text"]
    assert official.check(cfg, state, NOW)[0] == []                  # 같은 기사는 다시 안 알림
