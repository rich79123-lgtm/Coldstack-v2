"""The background engine: verification queue, campaign sending, reply/bounce sync and warmup."""
import hashlib
import random
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import ai, mailer, render
from db import ex, get_setting, log_event, now_iso, q, q1
from security import sign
from verify import verify_email

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0)


def parse_dt(s):
    if not s:
        return None
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def utc_midnight():
    n = utcnow()
    return datetime(n.year, n.month, n.day, tzinfo=timezone.utc).isoformat()


# ------------------------------------------------------------ verification

def verify_tick(batch=40):
    rows = q("SELECT id, email, provider_status FROM leads WHERE verify_status='pending' ORDER BY id LIMIT ?", (batch,))
    for r in rows:
        try:
            status, detail = verify_email(r["email"], r["provider_status"] or "")
        except Exception as e:
            status, detail = "risky", f"verification error: {e}"
        ex("UPDATE leads SET verify_status=?, verify_detail=? WHERE id=?", (status, detail, r["id"]))
    return len(rows)


# ------------------------------------------------------------ capacity

def ramp_limit(inbox: dict, today: date | None = None) -> int:
    today = today or utcnow().date()
    full = int(inbox["daily_limit"] or 0)
    days = int(inbox["ramp_days"] or 0)
    started = parse_dt(inbox["ramp_started"]) if inbox.get("ramp_started") else None
    if not days or not started:
        return full
    d = (today - started.date()).days
    if d >= days:
        return full
    start = min(int(inbox["ramp_start_volume"] or 5), full)
    return max(1, round(start + (full - start) * max(d, 0) / days))


def sent_today(inbox_id: int, kind="out") -> int:
    return q1("SELECT COUNT(*) n FROM messages WHERE inbox_id=? AND kind=? AND created_at>=?",
              (inbox_id, kind, utc_midnight()))["n"]


def capacity(inbox: dict) -> int:
    return max(0, ramp_limit(inbox) - sent_today(inbox["id"]))


def gap_ok(inbox: dict, now=None) -> bool:
    now = now or utcnow()
    last = parse_dt(inbox.get("last_sent_at"))
    if not last:
        return True
    gap = int(inbox.get("min_gap_seconds") or 0) * random.uniform(0.8, 1.3)
    return (now - last).total_seconds() >= gap


def in_window(c: dict, now=None) -> bool:
    now = now or utcnow()
    try:
        local = now.astimezone(ZoneInfo(c["timezone"] or "UTC"))
    except Exception:
        local = now
    if DAYS[local.weekday()] not in (c["send_days"] or "").split(","):
        return False
    start = time.fromisoformat(c["window_start"] or "00:00")
    end = time.fromisoformat(c["window_end"] or "23:59")
    return start <= local.time() <= end


# ------------------------------------------------------------ campaigns

def campaign_steps(campaign_id: int) -> list:
    steps = q("SELECT * FROM steps WHERE campaign_id=? ORDER BY position", (campaign_id,))
    for s in steps:
        s["variants"] = q("SELECT * FROM variants WHERE step_id=? ORDER BY label", (s["id"],))
    return steps


def is_suppressed(email: str) -> bool:
    domain = "@" + email.split("@")[-1]
    return bool(q1("SELECT 1 FROM suppression WHERE email IN (?, ?)", (email.lower(), domain.lower())))


def check_ready(c: dict):
    if not c or not c["list_id"]:
        raise ValueError("Campaign needs a lead list.")
    steps = campaign_steps(c["id"])
    if not steps or not any(v["body"].strip() for v in steps[0]["variants"]):
        raise ValueError("Campaign needs at least one step with an email body.")
    if not q1("SELECT 1 FROM campaign_inboxes WHERE campaign_id=?", (c["id"],)):
        raise ValueError("Attach at least one sending inbox.")


def enroll_new(c: dict) -> int:
    """Enrol every verified lead in the campaign's list that isn't enrolled yet."""
    statuses = ("valid", "risky") if c["include_risky"] else ("valid",)
    leads = q(f"SELECT id, email FROM leads WHERE list_id=? AND verify_status IN ({','.join('?' * len(statuses))}) "
              "AND id NOT IN (SELECT lead_id FROM enrollments WHERE campaign_id=?)",
              (c["list_id"], *statuses, c["id"]))
    n = 0
    now = now_iso()
    for lead in leads:
        if is_suppressed(lead["email"]):
            continue
        # don't email someone who is already in another active sequence
        if q1("SELECT 1 FROM enrollments e JOIN leads l ON l.id=e.lead_id WHERE l.email=? AND e.status='active' "
              "AND e.campaign_id<>?", (lead["email"], c["id"])):
            continue
        try:
            ex("INSERT INTO enrollments(campaign_id, lead_id, next_step, next_send_at, status, created_at) "
               "VALUES (?,?,0,?,'active',?)", (c["id"], lead["id"], now, now))
            n += 1
        except Exception:
            pass
    return n


def launch_campaign(campaign_id: int) -> int:
    c = q1("SELECT * FROM campaigns WHERE id=?", (campaign_id,))
    check_ready(c)
    n = enroll_new(c)
    ex("UPDATE campaigns SET status='active' WHERE id=?", (campaign_id,))
    log_event("campaign", f"Campaign {c['name']} launched: {n} new leads enrolled")
    return n


def pick_variant(step: dict, lead_id: int):
    vs = [v for v in step["variants"] if v["active"] and v["body"].strip()]
    if not vs:
        return None
    h = int(hashlib.md5(f"{lead_id}-{step['id']}".encode()).hexdigest(), 16)
    return vs[h % len(vs)]


def unsubscribe_url(lead_id: int) -> str:
    return get_setting("public_base_url").rstrip("/") + "/u/" + sign(f"l{lead_id}")


def compose(enrollment: dict, lead: dict, inbox: dict, step: dict, variant: dict, campaign: dict):
    seed = f"{step['id']}-{variant['id']}"
    body = render.render(variant["body"], lead, inbox, seed=seed)
    if variant["subject"].strip():
        subject = render.render(variant["subject"], lead, inbox, seed=seed)
    else:
        base = enrollment.get("thread_subject") or ""
        subject = base if base.lower().startswith("re:") else (f"Re: {base}" if base else "Following up")
    if inbox.get("signature"):
        body += "\n\n" + render.render(inbox["signature"], lead, inbox, seed=seed)
    unsub = unsubscribe_url(lead["id"])
    if campaign["unsubscribe_footer"]:
        body += render.footer(unsub, get_setting("sender_company"), get_setting("sender_postal_address"))
    html = None if campaign["plain_text"] else render.to_html(body)
    return subject, body, html, unsub


def send_tick(max_per_tick=25) -> int:
    if get_setting("send_engine_enabled") != "1":
        return 0
    sent = 0
    now = utcnow()
    for c in q("SELECT * FROM campaigns WHERE status='active'"):
        if not in_window(c, now):
            continue
        c_sent = q1("SELECT COUNT(*) n FROM messages WHERE campaign_id=? AND kind='out' AND created_at>=?",
                    (c["id"], utc_midnight()))["n"]
        if c_sent >= int(c["daily_cap"] or 0):
            continue
        inboxes = {i["id"]: i for i in q(
            "SELECT i.* FROM inboxes i JOIN campaign_inboxes ci ON ci.inbox_id=i.id "
            "WHERE ci.campaign_id=? AND i.status='active'", (c["id"],))}
        if not inboxes:
            continue
        steps = campaign_steps(c["id"])
        used = set()  # one send per inbox per tick keeps sending spread out
        due = q("SELECT * FROM enrollments WHERE campaign_id=? AND status='active' AND next_send_at<=? "
                "ORDER BY next_step DESC, next_send_at LIMIT 200", (c["id"], now.isoformat()))
        if not due and not c["autopilot"] and \
                not q1("SELECT 1 FROM enrollments WHERE campaign_id=? AND status='active'", (c["id"],)):
            ex("UPDATE campaigns SET status='completed' WHERE id=?", (c["id"],))
            log_event("campaign", f"Campaign {c['name']} completed")
            continue
        for e in due:
            if sent >= max_per_tick or c_sent >= int(c["daily_cap"]):
                break
            lead = q1("SELECT * FROM leads WHERE id=?", (e["lead_id"],))
            if not lead or is_suppressed(lead["email"]):
                ex("UPDATE enrollments SET status='unsubscribed' WHERE id=?", (e["id"],))
                continue
            if e["next_step"] >= len(steps):
                ex("UPDATE enrollments SET status='completed' WHERE id=?", (e["id"],))
                continue
            # choose inbox: sticky once assigned (keeps the thread in one mailbox)
            inbox = inboxes.get(e["inbox_id"]) if e["inbox_id"] else None
            if e["inbox_id"] and not inbox:
                continue  # assigned inbox paused/removed
            if inbox is None:
                candidates = [i for i in inboxes.values() if i["id"] not in used and gap_ok(i, now) and capacity(i) > 0]
                if not candidates:
                    continue
                inbox = max(candidates, key=capacity)
            elif inbox["id"] in used or not gap_ok(inbox, now) or capacity(inbox) <= 0:
                continue
            step = steps[e["next_step"]]
            variant = pick_variant(step, lead["id"])
            if not variant:
                _advance(e, steps, now)
                continue
            subject, body, html, unsub = compose(e, lead, inbox, step, variant, c)
            msg = mailer.build_message(inbox, lead["email"], subject, body, html,
                                       in_reply_to=e["last_message_id"] or "", references=e["references_ids"] or "",
                                       unsubscribe_url=unsub)
            used.add(inbox["id"])
            try:
                mailer.smtp_send(inbox, msg)
            except mailer.SendError as err:
                if err.hard_bounce:
                    _mark_bounce(e, lead, str(err))
                else:
                    ex("UPDATE inboxes SET last_error=?, last_sent_at=? WHERE id=?", (str(err)[:500], now.isoformat(), inbox["id"]))
                    log_event("send", f"{inbox['email']} → {lead['email']}: {err}", "error")
                    if "authentication" in str(err):
                        ex("UPDATE inboxes SET status='error' WHERE id=?", (inbox["id"],))
                continue
            mid = msg["Message-ID"]
            ex("INSERT INTO messages(kind, enrollment_id, campaign_id, lead_id, inbox_id, step_position, variant_id, "
               "message_id, in_reply_to, from_addr, to_addr, subject, body, is_read, created_at) "
               "VALUES ('out',?,?,?,?,?,?,?,?,?,?,?,?,1,?)",
               (e["id"], c["id"], lead["id"], inbox["id"], step["position"], variant["id"], mid,
                e["last_message_id"] or "", inbox["email"], lead["email"], subject, body, now.isoformat()))
            ex("UPDATE inboxes SET last_sent_at=?, last_error='' WHERE id=?", (now.isoformat(), inbox["id"]))
            refs = ((e["references_ids"] or "") + " " + mid).strip()
            ex("UPDATE enrollments SET inbox_id=?, thread_subject=COALESCE(NULLIF(thread_subject,''), ?), "
               "last_message_id=?, references_ids=? WHERE id=?", (inbox["id"], subject, mid, refs, e["id"]))
            e = q1("SELECT * FROM enrollments WHERE id=?", (e["id"],))
            _advance(e, steps, now)
            sent += 1
            c_sent += 1
    return sent


def _advance(e, steps, now):
    nxt = e["next_step"] + 1
    if nxt >= len(steps):
        ex("UPDATE enrollments SET next_step=?, status='completed' WHERE id=?", (nxt, e["id"]))
    else:
        when = now + timedelta(days=int(steps[nxt]["delay_days"] or 0))
        ex("UPDATE enrollments SET next_step=?, next_send_at=? WHERE id=?", (nxt, when.isoformat(), e["id"]))


def _mark_bounce(e, lead, detail):
    ex("UPDATE enrollments SET status='bounced' WHERE lead_id=? AND status='active'", (lead["id"],))
    ex("UPDATE leads SET verify_status='invalid', verify_detail=? WHERE email=?", (f"bounced: {detail[:200]}", lead["email"]))
    ex("INSERT OR IGNORE INTO suppression(email, reason, created_at) VALUES (?,?,?)", (lead["email"].lower(), "bounce", now_iso()))
    log_event("send", f"Bounce: {lead['email']} ({detail[:120]})", "warning")
    fire_webhook("bounce", {"email": lead["email"], "detail": detail[:300]})


def suppress(email_addr: str, reason: str):
    email_addr = email_addr.lower().strip()
    ex("INSERT OR IGNORE INTO suppression(email, reason, created_at) VALUES (?,?,?)", (email_addr, reason, now_iso()))
    ex("UPDATE enrollments SET status='unsubscribed' WHERE status='active' AND lead_id IN "
       "(SELECT id FROM leads WHERE email=?)", (email_addr,))
    fire_webhook("unsubscribe", {"email": email_addr, "reason": reason})


# ------------------------------------------------------------ inbound sync

def sync_tick() -> int:
    total = 0
    for inbox in q("SELECT * FROM inboxes WHERE status='active' AND imap_host IS NOT NULL AND imap_host<>''"):
        try:
            msgs, new_uid = mailer.fetch_new(inbox, int(inbox["imap_last_uid"]))
            for _, m in msgs:
                try:
                    process_inbound(inbox, m)
                    total += 1
                except Exception as e:
                    log_event("sync", f"{inbox['email']}: failed to process message: {e}", "error")
            ex("UPDATE inboxes SET imap_last_uid=? WHERE id=?", (new_uid, inbox["id"]))
            if inbox["warmup_enabled"]:
                mailer.rescue_from_spam(inbox)
        except Exception as e:
            ex("UPDATE inboxes SET last_error=? WHERE id=?", (f"IMAP: {e}"[:500], inbox["id"]))
            log_event("sync", f"{inbox['email']}: {e}", "error")
    return total


def process_inbound(inbox: dict, m):
    from_addr = mailer.addr(m.get("From"))
    subject = str(m.get("Subject") or "")
    mid = (m.get("Message-ID") or "").strip()
    if mid and q1("SELECT 1 FROM messages WHERE message_id=? AND kind IN ('in','warmup_in','bounce')", (mid,)):
        return
    body_full = mailer.text_of(m)
    body = mailer.strip_quoted(body_full) or body_full[:4000]
    now = now_iso()

    if m.get("X-Coldstack-Warmup"):
        ex("INSERT INTO messages(kind, inbox_id, message_id, from_addr, to_addr, subject, body, is_read, created_at) "
           "VALUES ('warmup_in',?,?,?,?,?,?,1,?)", (inbox["id"], mid, from_addr, inbox["email"], subject, body[:2000], now))
        if not subject.lower().startswith("re:") and random.random() < 0.4:
            _warmup_reply(inbox, from_addr, subject, mid)
        return

    refs = f"{m.get('In-Reply-To') or ''} {m.get('References') or ''}".split()
    is_bounce = ("mailer-daemon" in from_addr or "postmaster" in from_addr or
                 "delivery-status" in (m.get_content_type() + str(m.get("Content-Type", ""))).lower())
    if is_bounce:
        failed = (m.get("X-Failed-Recipients") or "").lower().strip()
        original = None
        for ref in refs + _message_ids_in(body_full):
            original = q1("SELECT * FROM messages WHERE message_id=? AND kind='out'", (ref,))
            if original:
                break
        lead = None
        if original:
            lead = q1("SELECT * FROM leads WHERE id=?", (original["lead_id"],))
        elif failed:
            lead = q1("SELECT * FROM leads WHERE email=?", (failed,))
        if lead:
            e = q1("SELECT * FROM enrollments WHERE lead_id=? ORDER BY id DESC", (lead["id"],))
            _mark_bounce(e, lead, subject)
            ex("INSERT INTO messages(kind, campaign_id, lead_id, inbox_id, message_id, from_addr, to_addr, subject, body, "
               "classification, is_read, created_at) VALUES ('bounce',?,?,?,?,?,?,?,?,'bounce',1,?)",
               (original["campaign_id"] if original else None, lead["id"], inbox["id"], mid, from_addr, inbox["email"],
                subject, body[:2000], now))
        return

    original = None
    for ref in refs:
        original = q1("SELECT * FROM messages WHERE message_id=? AND kind='out'", (ref,))
        if original:
            break
    if not original:
        original = q1("SELECT * FROM messages WHERE kind='out' AND to_addr=? AND inbox_id=? ORDER BY id DESC",
                      (from_addr, inbox["id"]))
    if not original:
        return  # unrelated mail in this mailbox — leave it alone

    label = ai.classify_reply(subject, body)
    in_id = ex("INSERT INTO messages(kind, enrollment_id, campaign_id, lead_id, inbox_id, message_id, in_reply_to, from_addr, "
       "to_addr, subject, body, classification, is_read, created_at) VALUES ('in',?,?,?,?,?,?,?,?,?,?,?,0,?)",
       (original["enrollment_id"], original["campaign_id"], original["lead_id"], inbox["id"], mid,
        original["message_id"], from_addr, inbox["email"], subject, body[:8000], label, now))
    lead = q1("SELECT * FROM leads WHERE id=?", (original["lead_id"],))
    if label == "unsubscribe":
        suppress(lead["email"], "replied: unsubscribe")
    elif label == "bounce":
        _mark_bounce(None, lead, subject)
    elif label == "out_of_office":
        pass  # keep the sequence going
    else:
        c = q1("SELECT * FROM campaigns WHERE id=?", (original["campaign_id"],))
        if c and c["stop_on_reply"]:
            ex("UPDATE enrollments SET status='replied' WHERE status='active' AND lead_id IN "
               "(SELECT id FROM leads WHERE email=?)", (lead["email"],))
    log_event("reply", f"Reply from {from_addr}: {label}")
    try:
        draft_for(in_id)
    except Exception as e:
        log_event("reply", f"Draft failed: {e}", "warning")
    fire_webhook("reply", {"label": label, "from": from_addr, "subject": subject, "body": body[:4000],
                           "campaign_id": original["campaign_id"], "lead": {k: lead.get(k) for k in (
                               "email", "first_name", "last_name", "title", "company", "linkedin_url")}})


def fire_webhook(event: str, payload: dict):
    url = get_setting("webhook_url")
    if not url:
        return
    try:
        import httpx
        httpx.post(url, json={"event": event, "at": now_iso(), **payload}, timeout=10)
    except Exception as e:
        log_event("webhook", f"{event}: {e}", "warning")


def _message_ids_in(text: str) -> list:
    import re
    return re.findall(r"<[^<>\s]+@[^<>\s]+>", text or "")


def send_manual_reply(in_message_id: int, body: str) -> str:
    m = q1("SELECT * FROM messages WHERE id=?", (in_message_id,))
    inbox = q1("SELECT * FROM inboxes WHERE id=?", (m["inbox_id"],))
    subject = m["subject"] if m["subject"].lower().startswith("re:") else f"Re: {m['subject']}"
    if inbox.get("signature"):
        body += "\n\n" + inbox["signature"]
    e = q1("SELECT * FROM enrollments WHERE id=?", (m["enrollment_id"],)) if m["enrollment_id"] else None
    refs = (e["references_ids"] if e else "") or ""
    msg = mailer.build_message(inbox, m["from_addr"], subject, body, in_reply_to=m["message_id"], references=refs)
    mailer.smtp_send(inbox, msg)
    ex("INSERT INTO messages(kind, enrollment_id, campaign_id, lead_id, inbox_id, message_id, in_reply_to, from_addr, "
       "to_addr, subject, body, is_read, classification, created_at) VALUES ('out',?,?,?,?,?,?,?,?,?,?,1,'manual',?)",
       (m["enrollment_id"], m["campaign_id"], m["lead_id"], inbox["id"], msg["Message-ID"], m["message_id"],
        inbox["email"], m["from_addr"], subject, body, now_iso()))
    if e:
        ex("UPDATE enrollments SET last_message_id=?, references_ids=? WHERE id=?",
           (msg["Message-ID"], (refs + " " + msg["Message-ID"]).strip(), e["id"]))
    return msg["Message-ID"]


# ------------------------------------------------------------ warmup

WARMUP_SUBJECTS = ["quick catch-up", "notes from today", "thursday plan", "following up on our chat", "small update",
                   "question about next week", "agenda thoughts", "thanks again"]
WARMUP_BODIES = ["Hi {name},\n\nJust wanted to say thanks for the help earlier. Let me know if Thursday still works.\n\nBest,\n{me}",
                 "Hey {name},\n\nI've pulled together a few notes from the call. Happy to go through them whenever suits.\n\n{me}",
                 "Hi {name},\n\nAre we still on for the review next week? I can move it if needed.\n\nCheers,\n{me}",
                 "Morning {name},\n\nQuick one: did you get a chance to look at the draft? No rush.\n\n{me}"]
WARMUP_REPLIES = ["Thanks {name}, that works for me.", "Got it, appreciate it!", "Sounds good, speak then.",
                  "Yes, all good on my side. Thanks!"]


def warmup_tick() -> int:
    if get_setting("warmup_engine_enabled") != "1":
        return 0
    pool = q("SELECT * FROM inboxes WHERE status='active' AND warmup_enabled=1")
    if len(pool) < 2:
        return 0
    sent = 0
    now = utcnow()
    for inbox in pool:
        if sent_today(inbox["id"], "warmup_out") >= int(inbox["warmup_daily"] or 0):
            continue
        last = q1("SELECT created_at FROM messages WHERE inbox_id=? AND kind='warmup_out' ORDER BY id DESC", (inbox["id"],))
        if last and (now - parse_dt(last["created_at"])).total_seconds() < 1800 * random.uniform(0.7, 1.3):
            continue
        peer = random.choice([p for p in pool if p["id"] != inbox["id"]])
        name = (peer["from_name"] or peer["email"].split("@")[0]).split(" ")[0]
        me = (inbox["from_name"] or inbox["email"].split("@")[0]).split(" ")[0]
        msg = mailer.build_message(inbox, peer["email"], random.choice(WARMUP_SUBJECTS),
                                   random.choice(WARMUP_BODIES).format(name=name, me=me),
                                   extra_headers={"X-Coldstack-Warmup": "1"})
        try:
            mailer.smtp_send(inbox, msg)
        except mailer.SendError as e:
            log_event("warmup", f"{inbox['email']}: {e}", "error")
            continue
        ex("INSERT INTO messages(kind, inbox_id, message_id, from_addr, to_addr, subject, body, is_read, created_at) "
           "VALUES ('warmup_out',?,?,?,?,?,?,1,?)", (inbox["id"], msg["Message-ID"], inbox["email"], peer["email"],
                                                     msg["Subject"], "", now.isoformat()))
        sent += 1
    return sent


def _warmup_reply(inbox, to_addr, subject, mid):
    name = to_addr.split("@")[0].split(".")[0].title()
    msg = mailer.build_message(inbox, to_addr, f"Re: {subject}", random.choice(WARMUP_REPLIES).format(name=name),
                               in_reply_to=mid, extra_headers={"X-Coldstack-Warmup": "1"})
    try:
        mailer.smtp_send(inbox, msg)
        ex("INSERT INTO messages(kind, inbox_id, message_id, from_addr, to_addr, subject, body, is_read, created_at) "
           "VALUES ('warmup_out',?,?,?,?,?,'',1,?)", (inbox["id"], msg["Message-ID"], inbox["email"], to_addr,
                                                     msg["Subject"], now_iso()))
    except mailer.SendError:
        pass


# ------------------------------------------------------------ lead storage

def add_leads(list_id: int, rows: list, source: str) -> int:
    import json
    n = 0
    for r in rows:
        email = (r.get("email") or "").strip().lower()
        if not email:
            continue
        try:
            ex("INSERT INTO leads(list_id, email, first_name, last_name, title, company, company_domain, industry, "
               "company_size, location, linkedin_url, custom, source, provider_status, provider_id, created_at) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (list_id, email, r.get("first_name", ""), r.get("last_name", ""), r.get("title", ""), r.get("company", ""),
                r.get("company_domain", "") or email.split("@")[1], r.get("industry", ""), r.get("company_size", ""),
                r.get("location", ""), r.get("linkedin_url", ""), json.dumps(r.get("custom") or {}), source,
                r.get("provider_status", ""), r.get("provider_id", ""), now_iso()))
            n += 1
        except Exception:
            pass  # duplicate in this list
    return n


# ------------------------------------------------------------ autopilot

def autopilot_tick() -> int:
    """For each active autopilot campaign: once a day pull fresh leads from the provider (never the same
    person twice), let verification run, and enrol every newly verified lead."""
    import json
    from providers import ProviderError, get_provider
    added_total = 0
    today = utcnow().date().isoformat()
    for c in q("SELECT * FROM campaigns WHERE status='active' AND autopilot=1"):
        enroll_new(c)  # picks up leads verified since the last tick
        if (c["autopilot_last_run"] or "")[:10] == today:
            continue
        in_list = q1("SELECT COUNT(*) n FROM leads WHERE list_id=?", (c["list_id"],))["n"]
        want = min(int(c["autopilot_daily"] or 0), max(0, int(c["autopilot_max_total"] or 0) - in_list))
        note = ""
        page = int(c["autopilot_page"] or 1)
        fresh = []
        if want > 0:
            filters = json.loads(c["autopilot_filters"] or "{}")
            try:
                prov = get_provider()
                pages_tried = 0
                while len(fresh) < want and pages_tried < 10:
                    res = prov.search(filters, page, 100)
                    pages_tried += 1
                    if not res.rows:
                        note = "Saved search is used up: widen the filters to keep finding new leads."
                        break
                    for r in res.rows:
                        if r.get("provider_status") == "no email":
                            continue
                        pid = r.get("provider_id") or ""
                        if pid and q1("SELECT 1 FROM leads WHERE provider_id=?", (pid,)):
                            continue
                        if r.get("email") and (is_suppressed(r["email"]) or q1("SELECT 1 FROM leads WHERE email=?", (r["email"].lower(),))):
                            continue
                        fresh.append(r)
                        if len(fresh) >= want:
                            break
                    if len(fresh) < want or len(res.rows) < 100:
                        page += 1
                revealed = prov.reveal(fresh) if fresh else []
                added = add_leads(c["list_id"], revealed, prov.name)
                added_total += added
                note = note or f"{today}: found {len(fresh)}, {len(revealed)} had emails, {added} added."
            except ProviderError as e:
                note = f"{today}: lead search failed: {e}"
                log_event("autopilot", f"{c['name']}: {e}", "error")
        else:
            note = f"{today}: list is at its {c['autopilot_max_total']}-lead limit; no new leads pulled."
        ex("UPDATE campaigns SET autopilot_last_run=?, autopilot_page=?, autopilot_note=? WHERE id=?",
           (now_iso(), page, note, c["id"]))
        log_event("autopilot", f"{c['name']}: {note}")
    return added_total


# ------------------------------------------------------------ reply drafts

def draft_for(message_id: int):
    """Write an AI reply draft for an inbound message; in 'auto' mode, send it for interested leads."""
    m = q1("SELECT * FROM messages WHERE id=?", (message_id,))
    if not m or not m["campaign_id"]:
        return
    c = q1("SELECT * FROM campaigns WHERE id=?", (m["campaign_id"],))
    if not c or (c["reply_mode"] or "off") == "off" or m["classification"] not in ("interested", "other"):
        return
    lead = q1("SELECT * FROM leads WHERE id=?", (m["lead_id"],)) or {}
    inbox = q1("SELECT * FROM inboxes WHERE id=?", (m["inbox_id"],)) or {}
    thread = q("SELECT kind, body FROM messages WHERE lead_id=? AND kind IN ('out','in') ORDER BY id", (m["lead_id"],))
    draft = ai.draft_reply(lead, inbox, thread, c["reply_context"] or "", m["classification"])
    ex("UPDATE messages SET draft=?, draft_status='ready' WHERE id=?", (draft, message_id))
    if c["reply_mode"] == "auto" and m["classification"] == "interested":
        try:
            send_manual_reply(message_id, draft)
            ex("UPDATE messages SET draft_status='sent', is_read=1 WHERE id=?", (message_id,))
            log_event("reply", f"Auto-replied to {m['from_addr']}")
        except Exception as e:
            log_event("reply", f"Auto-reply to {m['from_addr']} failed: {e}", "error")
