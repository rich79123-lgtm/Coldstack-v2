"""End-to-end test: demo leads -> verification -> campaign -> real SMTP send to a local sink -> reply handling."""
import os
import random
import tempfile
import time
from datetime import datetime, timedelta, timezone

import pytest


import ai, engine, render  # noqa: E402
from db import ex, init_db, now_iso, q, q1, set_setting  # noqa: E402
from providers import DemoProvider  # noqa: E402
from security import encrypt, sign, unsign  # noqa: E402


@pytest.fixture(scope="module")
def smtp_sink():
    from aiosmtpd.controller import Controller

    class Handler:
        def __init__(self):
            self.messages = []

        async def handle_DATA(self, server, session, envelope):
            self.messages.append(envelope)
            return "250 OK"
    h = Handler()
    ctl = Controller(h, hostname="127.0.0.1", port=8025)
    ctl.start()
    yield h
    ctl.stop()


def setup_module():
    init_db()


def test_spintax_and_merge():
    lead = {"id": 1, "first_name": "Ana", "company": "", "custom": '{"icebreaker": "Loved your talk"}'}
    out = render.render("{Hi|Hey} {{first_name}} at {{company|your team}}. {{icebreaker}}", lead)
    assert out.split(" ")[0] in ("Hi", "Hey")
    assert "Ana at your team. Loved your talk" in out
    # deterministic per lead
    assert out == render.render("{Hi|Hey} {{first_name}} at {{company|your team}}. {{icebreaker}}", lead)


def test_heuristic_icp_and_classifier():
    f = ai._clean_filters(ai._heuristic_icp("Heads of Quality at pharma companies in the UK with 50-500 employees"))
    assert "United Kingdom" in f["locations"] and f["titles"] and "51,200" in f["employee_ranges"]
    assert ai._heuristic_classify("Automatic reply", "I am out of the office until Monday") == "out_of_office"
    assert ai._heuristic_classify("Re: hi", "please remove me from your list") == "unsubscribe"
    assert ai._heuristic_classify("Re: hi", "Sounds good, can you send pricing?") == "interested"


def test_sign_roundtrip():
    t = sign("l42")
    assert unsign(t) == "l42" and unsign(t[:-1] + "x") is None


def test_ramp():
    started = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    inbox = {"daily_limit": 40, "ramp_days": 14, "ramp_start_volume": 5, "ramp_started": started}
    assert 20 <= engine.ramp_limit(inbox) <= 24
    inbox["ramp_days"] = 0
    assert engine.ramp_limit(inbox) == 40


def test_full_flow(smtp_sink):
    set_setting("public_base_url", "http://localhost:8000")
    # leads
    lid = ex("INSERT INTO lists(name, created_at) VALUES ('t', ?)", (now_iso(),))
    rows = DemoProvider().search({"titles": ["Head of Quality"]}, 1, 5).rows
    from main import add_leads
    assert add_leads(lid, rows, "demo") == 5
    ex("INSERT INTO leads(list_id, email, first_name, created_at, provider_status) VALUES (?,?,?,?,?)",
       (lid, "bad@@x", "Bad", now_iso(), ""))
    engine.verify_tick(50)
    assert q1("SELECT COUNT(*) n FROM leads WHERE verify_status='valid'")["n"] == 5
    assert q1("SELECT verify_status s FROM leads WHERE email='bad@@x'")["s"] == "invalid"
    # inbox pointing at the local SMTP sink, no ramp, no gap
    iid = ex("INSERT INTO inboxes(email, from_name, smtp_host, smtp_port, smtp_security, smtp_user, smtp_pass, daily_limit, "
             "ramp_days, min_gap_seconds, warmup_enabled, created_at, ramp_started) VALUES "
             "('me@sender.example','Me Sender','127.0.0.1',8025,'none','me@sender.example',?,100,0,0,0,?,?)",
             (encrypt(""), now_iso(), now_iso()))
    cid = ex("INSERT INTO campaigns(name, list_id, timezone, window_start, window_end, send_days, created_at) "
             "VALUES ('c', ?, 'UTC', '00:00', '23:59', 'mon,tue,wed,thu,fri,sat,sun', ?)", (lid, now_iso()))
    ex("INSERT INTO campaign_inboxes VALUES (?,?)", (cid, iid))
    s1 = ex("INSERT INTO steps(campaign_id, position, delay_days) VALUES (?,0,0)", (cid,))
    ex("INSERT INTO variants(step_id, label, subject, body) VALUES (?,'A','hi {{first_name}}','{Hi|Hey} {{first_name}}, A')", (s1,))
    ex("INSERT INTO variants(step_id, label, subject, body) VALUES (?,'B','hello {{first_name}}','Hello {{first_name}}, B')", (s1,))
    s2 = ex("INSERT INTO steps(campaign_id, position, delay_days) VALUES (?,1,0)", (cid,))
    ex("INSERT INTO variants(step_id, label, subject, body) VALUES (?,'A','','Following up {{first_name}}')", (s2,))

    assert engine.launch_campaign(cid) == 5
    sent = 0
    for _ in range(20):  # one send per inbox per tick
        sent += engine.send_tick()
    assert sent == 10, sent  # 5 leads x 2 steps
    assert len(smtp_sink.messages) == 10
    raw = smtp_sink.messages[-1].content.decode()
    assert "List-Unsubscribe" in raw and "In-Reply-To" in raw and "Subject: Re: h" in raw
    assert q1("SELECT COUNT(*) n FROM enrollments WHERE status='completed'")["n"] == 5

    # simulate an interested reply to the first lead's thread
    import email as em
    out = q1("SELECT * FROM messages WHERE kind='out' ORDER BY id LIMIT 1")
    reply = em.message_from_string(
        f"From: x <{out['to_addr']}>\nTo: me@sender.example\nSubject: Re: {out['subject']}\n"
        f"Message-ID: <r1@x>\nIn-Reply-To: {out['message_id']}\n\nYes, interested! Can you send pricing?\n\nOn Mon wrote:\n> old")
    inbox = q1("SELECT * FROM inboxes WHERE id=?", (iid,))
    engine.process_inbound(inbox, reply)
    r = q1("SELECT * FROM messages WHERE kind='in'")
    assert r["classification"] == "interested" and "old" not in r["body"]

    # unsubscribe suppresses future sends
    lead = q1("SELECT * FROM leads WHERE email=?", (out["to_addr"],))
    engine.suppress(lead["email"], "test")
    assert engine.is_suppressed(lead["email"])


def test_autopilot_drafts_and_import():
    import json
    from fastapi.testclient import TestClient
    import main
    set_setting("lead_provider", "demo")
    client = TestClient(main.app)
    pack = {"name": "AP test", "settings": {"timezone": "UTC", "window_start": "00:00", "window_end": "23:59",
                                           "send_days": "mon,tue,wed,thu,fri,sat,sun"},
            "autopilot": {"enabled": True, "daily_new_leads": 7, "max_total_leads": 10,
                          "filters": {"titles": ["Owner"], "keywords": ["mobile detailing"]}},
            "replies": {"mode": "draft", "context": "Free beta. Booking link: https://cal.example/r"},
            "steps": [{"delay_days": 0, "variants": [{"label": "A", "subject": "hi", "body": "Hi {{first_name}}"}]}]}
    r = client.post("/campaigns/import", data={"pack": json.dumps(pack)}, follow_redirects=False)
    assert r.status_code == 303
    c = q1("SELECT * FROM campaigns WHERE name='AP test'")
    assert c["autopilot"] == 1 and c["autopilot_daily"] == 7 and q1("SELECT COUNT(*) n FROM steps WHERE campaign_id=?", (c["id"],))["n"] == 1
    assert engine.launch_campaign(c["id"]) == 0          # list starts empty
    assert engine.autopilot_tick() == 7                  # day 1: 7 new leads
    assert engine.autopilot_tick() == 0                  # same day: nothing more
    ex("UPDATE campaigns SET autopilot_last_run='2000-01-01' WHERE id=?", (c["id"],))
    assert engine.autopilot_tick() == 3                  # next day: capped by max_total (10)
    ids = [r["provider_id"] for r in q("SELECT provider_id FROM leads WHERE list_id=?", (c["list_id"],))]
    assert len(ids) == len(set(ids)) == 10               # never the same person twice
    engine.verify_tick(100)
    engine.autopilot_tick()                              # enrols newly verified leads
    assert q1("SELECT COUNT(*) n FROM enrollments WHERE campaign_id=?", (c["id"],))["n"] == 10
    # an interested reply gets an AI (fallback) draft containing the booking link
    lead = q1("SELECT * FROM leads WHERE list_id=? LIMIT 1", (c["list_id"],))
    e = q1("SELECT * FROM enrollments WHERE lead_id=?", (lead["id"],))
    mid = ex("INSERT INTO messages(kind, enrollment_id, campaign_id, lead_id, inbox_id, message_id, from_addr, to_addr, subject, "
             "body, classification, created_at) VALUES ('in',?,?,?,?,'<x@y>',?,'me@x','Re: hi','Yes keen, tell me more','interested',?)",
             (e["id"], c["id"], lead["id"], 1, lead["email"], now_iso()))
    engine.draft_for(mid)
    m = q1("SELECT * FROM messages WHERE id=?", (mid,))
    assert m["draft_status"] == "ready" and "https://cal.example/r" in m["draft"]
    assert client.get(f"/unibox?msg={mid}").status_code == 200
    assert client.get(f"/campaigns/{c['id']}").status_code == 200


"""Apollo adapter against a response captured from the live API (search) and the documented bulk_match shape."""
import httpx
from providers import ApolloProvider

SEARCH = {"total_entries": 4839, "people": [
    {"id": "60ccac716a303900010f16ad", "first_name": "Claire", "last_name_obfuscated": "Wi***n",
     "title": "Head of Quality Assurance: Director of QA", "has_email": True,
     "organization": {"name": "TMC Pharma Services Ltd", "has_industry": True}},
    {"id": "66f852bc7a9fc200011a6cfb", "first_name": "Neetu", "last_name_obfuscated": "Bh***a",
     "title": "Director, Head of Quality Assurance", "has_email": False, "organization": {"name": "MUFG"}}]}
MATCH = {"status": "success", "matches": [{"id": "60ccac716a303900010f16ad", "first_name": "Claire", "last_name": "Wilson",
          "email": "claire@tmcpharma.example", "email_status": "verified", "title": "Head of QA", "city": "London",
          "country": "United Kingdom", "linkedin_url": "http://linkedin.com/in/x",
          "organization": {"name": "TMC Pharma Services Ltd", "primary_domain": "tmcpharma.com",
                           "industry": "pharmaceuticals", "estimated_num_employees": 120}}]}


def test_apollo(monkeypatch):
    calls = []

    def fake_post(url, params=None, json=None, headers=None, timeout=None):
        calls.append((url, params, json))
        return httpx.Response(200, json=SEARCH if "api_search" in url else MATCH)
    monkeypatch.setattr(httpx, "post", fake_post)
    p = ApolloProvider("k")
    res = p.search({"titles": ["Head of Quality", "QA Director"], "locations": ["United Kingdom"],
                    "employee_ranges": ["51,200"]}, 1, 25)
    url, params, _ = calls[0]
    assert ("person_titles[]", "Head of Quality") in params and ("person_titles[]", "QA Director") in params
    assert ("person_locations[]", "United Kingdom") in params and ("include_similar_titles", "true") in params
    assert res.total == 4839 and res.rows[0]["last_name"] == "Wi***n" and res.rows[1]["provider_status"] == "no email"
    out = p.reveal(res.rows)
    _, params, body = calls[1]
    assert body == {"details": [{"id": "60ccac716a303900010f16ad"}]}  # the no-email person was skipped
    assert ("reveal_personal_emails", "false") in params
    assert out[0]["email"] == "claire@tmcpharma.example" and out[0]["last_name"] == "Wilson"
    assert out[0]["company_domain"] == "tmcpharma.com" and out[0]["provider_status"] == "verified"


