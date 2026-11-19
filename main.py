"""Coldstack web app: pages + JSON API. Run with `python run.py`."""
import base64
import csv
import io
import json
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from jinja2 import DictLoader, Environment, select_autoescape

from assets import STATIC, TEMPLATES

import ai, engine, mailer, worker
from config import APP_PASSWORD, BASE_DIR, HOSTED, SETTING_DEFAULTS
from db import all_settings, ex, get_setting, init_db, now_iso, q, q1, set_setting
from providers import ProviderError, get_provider
from providers import blank_row
from security import encrypt, unsign


@asynccontextmanager
async def lifespan(app):
    init_db()
    worker.start()
    yield
    worker.stop()


app = FastAPI(title="Coldstack", lifespan=lifespan,
              description="Self-hosted cold email platform: lead search, verification, inboxes, sequences, master inbox.")
templates = Jinja2Templates(env=Environment(loader=DictLoader(TEMPLATES), autoescape=select_autoescape(["html"])))


@app.get("/static/{name}")
def static_file(name: str):
    if name not in STATIC:
        raise HTTPException(404)
    mt = "text/css" if name.endswith(".css") else "application/javascript"
    return Response(STATIC[name], media_type=mt, headers={"Cache-Control": "public, max-age=3600"})


templates.env.filters["fromjson"] = lambda s: json.loads(s or "{}")


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    path = request.url.path
    if HOSTED and not APP_PASSWORD and not path.startswith("/static"):
        return Response("Coldstack is online but locked: add an APP_PASSWORD variable in your hosting settings, "
                        "then redeploy.", 503)
    if APP_PASSWORD and not (path.startswith("/u/") or path.startswith("/static")):
        hdr = request.headers.get("authorization", "")
        ok = False
        if hdr.startswith("Basic "):
            try:
                _, _, pwd = base64.b64decode(hdr[6:]).decode().partition(":")
                ok = secrets.compare_digest(pwd, APP_PASSWORD)
            except Exception:
                ok = False
        if not ok:
            return Response("Authentication required", 401, {"WWW-Authenticate": 'Basic realm="Coldstack"'})
    return await call_next(request)


def page(request, name, **ctx):
    ctx.setdefault("unread", q1("SELECT COUNT(*) n FROM messages WHERE kind='in' AND is_read=0")["n"])
    ctx.setdefault("provider", get_setting("lead_provider"))
    ctx.setdefault("ai_on", bool(get_setting("anthropic_api_key")))
    return templates.TemplateResponse(request, name, ctx)


def back(url, msg=None, level="ok"):
    sep = "&" if "?" in url else "?"
    from urllib.parse import quote
    return RedirectResponse(f"{url}{sep}{level}={quote(msg)}" if msg else url, status_code=303)


# ================================================================ dashboard

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    since = engine.utc_midnight()
    stats = {
        "sent_today": q1("SELECT COUNT(*) n FROM messages WHERE kind='out' AND created_at>=?", (since,))["n"],
        "sent_total": q1("SELECT COUNT(*) n FROM messages WHERE kind='out'")["n"],
        "replies": q1("SELECT COUNT(DISTINCT lead_id) n FROM messages WHERE kind='in'")["n"],
        "interested": q1("SELECT COUNT(DISTINCT lead_id) n FROM messages WHERE kind='in' AND classification='interested'")["n"],
        "bounces": q1("SELECT COUNT(*) n FROM enrollments WHERE status='bounced'")["n"],
        "contacted": q1("SELECT COUNT(DISTINCT lead_id) n FROM messages WHERE kind='out'")["n"],
        "leads": q1("SELECT COUNT(*) n FROM leads")["n"],
        "pending_verify": q1("SELECT COUNT(*) n FROM leads WHERE verify_status='pending'")["n"],
    }
    c = max(stats["contacted"], 1)
    stats["reply_rate"] = round(100 * stats["replies"] / c, 1)
    stats["bounce_rate"] = round(100 * stats["bounces"] / c, 1)
    stats["positive_rate"] = round(100 * stats["interested"] / c, 1)
    daily = q("SELECT substr(created_at,1,10) d, SUM(kind='out') sent, SUM(kind='in') replies FROM messages "
              "WHERE created_at >= date('now','-13 days') GROUP BY d ORDER BY d")
    inboxes = q("SELECT * FROM inboxes ORDER BY id")
    for i in inboxes:
        i["today_limit"] = engine.ramp_limit(i)
        i["sent_today"] = engine.sent_today(i["id"])
    return page(request, "dashboard.html", stats=stats, campaigns=campaign_rows(), inboxes=inboxes, daily=daily,
                events=q("SELECT * FROM events ORDER BY id DESC LIMIT 12"), worker=worker.status)


def campaign_rows():
    rows = q("SELECT c.*, l.name list_name FROM campaigns c LEFT JOIN lists l ON l.id=c.list_id ORDER BY c.id DESC")
    for r in rows:
        cid = r["id"]
        r["enrolled"] = q1("SELECT COUNT(*) n FROM enrollments WHERE campaign_id=?", (cid,))["n"]
        r["active"] = q1("SELECT COUNT(*) n FROM enrollments WHERE campaign_id=? AND status='active'", (cid,))["n"]
        r["sent"] = q1("SELECT COUNT(*) n FROM messages WHERE campaign_id=? AND kind='out'", (cid,))["n"]
        r["contacted"] = q1("SELECT COUNT(DISTINCT lead_id) n FROM messages WHERE campaign_id=? AND kind='out'", (cid,))["n"]
        r["replies"] = q1("SELECT COUNT(DISTINCT lead_id) n FROM messages WHERE campaign_id=? AND kind='in'", (cid,))["n"]
        r["interested"] = q1("SELECT COUNT(DISTINCT lead_id) n FROM messages WHERE campaign_id=? AND kind='in' "
                             "AND classification='interested'", (cid,))["n"]
        r["bounced"] = q1("SELECT COUNT(*) n FROM enrollments WHERE campaign_id=? AND status='bounced'", (cid,))["n"]
        r["reply_rate"] = round(100 * r["replies"] / r["contacted"], 1) if r["contacted"] else 0
    return rows


# ================================================================ leads

@app.get("/leads", response_class=HTMLResponse)
def leads_home(request: Request):
    lists = q("SELECT l.*, (SELECT COUNT(*) FROM leads WHERE list_id=l.id) n, "
              "(SELECT COUNT(*) FROM leads WHERE list_id=l.id AND verify_status='valid') valid, "
              "(SELECT COUNT(*) FROM leads WHERE list_id=l.id AND verify_status='risky') risky, "
              "(SELECT COUNT(*) FROM leads WHERE list_id=l.id AND verify_status='invalid') invalid, "
              "(SELECT COUNT(*) FROM leads WHERE list_id=l.id AND verify_status='pending') pending "
              "FROM lists l ORDER BY l.id DESC")
    return page(request, "leads.html", lists=lists)


@app.get("/leads/search", response_class=HTMLResponse)
def lead_search_page(request: Request):
    return page(request, "lead_search.html", lists=q("SELECT * FROM lists ORDER BY id DESC"),
                seniorities=ai.SENIORITIES, ranges=ai.EMPLOYEE_RANGES)


@app.post("/api/icp")
async def api_icp(request: Request):
    body = await request.json()
    return ai.parse_icp(body.get("description", ""))


@app.post("/api/search")
async def api_search(request: Request):
    body = await request.json()
    try:
        prov = get_provider()
        res = prov.search(body.get("filters", {}), int(body.get("page", 1)), int(body.get("per_page", 25)))
    except ProviderError as e:
        return JSONResponse({"error": str(e)}, 400)
    return {"rows": res.rows, "total": res.total, "page": res.page, "note": res.note, "provider": prov.name,
            "charges_on_reveal": prov.charges_on_reveal}


@app.post("/api/save")
async def api_save(request: Request):
    """Reveal emails for the chosen rows (or the first N pages of results) and save them to a list."""
    body = await request.json()
    prov = get_provider()
    rows = body.get("rows") or []
    try:
        if body.get("count"):
            want, page_no, rows = int(body["count"]), 1, []
            while len(rows) < want and page_no <= 50:
                res = prov.search(body.get("filters", {}), page_no, min(100, want))
                if not res.rows:
                    break
                rows += res.rows
                page_no += 1
            rows = rows[:want]
        revealed = prov.reveal(rows)
    except ProviderError as e:
        return JSONResponse({"error": str(e)}, 400)
    list_id = body.get("list_id")
    if not list_id:
        list_id = ex("INSERT INTO lists(name, description, created_at) VALUES (?,?,?)",
                     (body.get("list_name") or "New list", body.get("summary", ""), now_iso()))
    added = add_leads(int(list_id), revealed, source=prov.name)
    return {"list_id": list_id, "requested": len(rows), "with_email": len(revealed), "added": added}


def add_leads(list_id: int, rows: list, source: str) -> int:
    n = 0
    for r in rows:
        email = (r.get("email") or "").strip().lower()
        if not email:
            continue
        custom = r.get("custom") or {}
        try:
            ex("INSERT INTO leads(list_id, email, first_name, last_name, title, company, company_domain, industry, "
               "company_size, location, linkedin_url, custom, source, provider_status, created_at) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (list_id, email, r.get("first_name", ""), r.get("last_name", ""), r.get("title", ""), r.get("company", ""),
                r.get("company_domain", "") or email.split("@")[1], r.get("industry", ""), r.get("company_size", ""),
                r.get("location", ""), r.get("linkedin_url", ""), json.dumps(custom), source,
                r.get("provider_status", ""), now_iso()))
            n += 1
        except Exception:
            pass  # duplicate in this list
    return n


CSV_ALIASES = {
    "email": ["email", "email address", "e-mail", "work email", "work_email"],
    "first_name": ["first name", "first_name", "firstname", "first"],
    "last_name": ["last name", "last_name", "lastname", "surname", "last"],
    "title": ["title", "job title", "job_title", "position", "role"],
    "company": ["company", "company name", "company_name", "organization", "organisation", "account"],
    "company_domain": ["domain", "website", "company domain", "company_domain", "company website"],
    "industry": ["industry"], "company_size": ["company size", "employees", "headcount", "company_size"],
    "location": ["location", "city", "country"], "linkedin_url": ["linkedin", "linkedin url", "linkedin_url", "person linkedin url"],
}


@app.post("/leads/import")
async def import_csv(file: UploadFile = File(...), list_name: str = Form(""), list_id: str = Form("")):
    raw = (await file.read()).decode("utf-8-sig", "replace")
    reader = csv.DictReader(io.StringIO(raw))
    mapping = {}
    for col in reader.fieldnames or []:
        key = col.strip().lower()
        for field, aliases in CSV_ALIASES.items():
            if key in aliases and field not in mapping.values():
                mapping[col] = field
                break
    if "email" not in mapping.values():
        return back("/leads", "CSV needs an 'email' column", "err")
    rows = []
    for rec in reader:
        r = blank_row()
        custom = {}
        for col, val in rec.items():
            if col in mapping:
                r[mapping[col]] = (val or "").strip()
            elif col:
                custom[col.strip().lower().replace(" ", "_")] = (val or "").strip()
        r["custom"] = custom
        rows.append(r)
    if not list_id:
        list_id = ex("INSERT INTO lists(name, description, created_at) VALUES (?,?,?)",
                     (list_name or file.filename, "CSV import", now_iso()))
    n = add_leads(int(list_id), rows, "csv")
    return back(f"/lists/{list_id}", f"Imported {n} leads. Verification is running in the background.")


@app.get("/lists/{list_id}", response_class=HTMLResponse)
def view_list(request: Request, list_id: int, status: str = ""):
    lst = q1("SELECT * FROM lists WHERE id=?", (list_id,))
    if not lst:
        raise HTTPException(404)
    where, params = "list_id=?", [list_id]
    if status:
        where += " AND verify_status=?"
        params.append(status)
    leads = q(f"SELECT * FROM leads WHERE {where} ORDER BY id LIMIT 1000", params)
    counts = {r["verify_status"]: r["n"] for r in q("SELECT verify_status, COUNT(*) n FROM leads WHERE list_id=? "
                                                     "GROUP BY verify_status", (list_id,))}
    return page(request, "list.html", lst=lst, leads=leads, counts=counts, status=status)


@app.get("/lists/{list_id}/export")
def export_list(list_id: int):
    rows = q("SELECT email, first_name, last_name, title, company, company_domain, industry, company_size, location, "
             "linkedin_url, verify_status, verify_detail FROM leads WHERE list_id=?", (list_id,))
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()) if rows else ["email"])
    w.writeheader()
    w.writerows(rows)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename=list-{list_id}.csv"})


@app.post("/lists/{list_id}/reverify")
def reverify(list_id: int):
    ex("UPDATE leads SET verify_status='pending' WHERE list_id=? AND verify_status<>'invalid'", (list_id,))
    return back(f"/lists/{list_id}", "Re-verification queued")


@app.post("/lists/{list_id}/delete")
def delete_list(list_id: int):
    if q1("SELECT 1 FROM campaigns WHERE list_id=? AND status='active'", (list_id,)):
        return back(f"/lists/{list_id}", "List is used by an active campaign", "err")
    ex("DELETE FROM lists WHERE id=?", (list_id,))
    return back("/leads", "List deleted")


# ================================================================ inboxes

@app.get("/inboxes", response_class=HTMLResponse)
def inboxes_page(request: Request):
    rows = q("SELECT * FROM inboxes ORDER BY id")
    for i in rows:
        i["today_limit"] = engine.ramp_limit(i)
        i["sent_today"] = engine.sent_today(i["id"])
        i["warmup_today"] = engine.sent_today(i["id"], "warmup_out")
        i["domain"] = i["email"].split("@")[1]
    return page(request, "inboxes.html", inboxes=rows, presets=mailer.PRESETS)


@app.post("/inboxes")
def add_inbox(email: str = Form(...), from_name: str = Form(""), preset: str = Form("custom"),
              smtp_host: str = Form(""), smtp_port: int = Form(587), smtp_security: str = Form("starttls"),
              password: str = Form(""), imap_host: str = Form(""), imap_port: int = Form(993),
              daily_limit: int = Form(30), ramp_days: int = Form(14), warmup_enabled: str = Form("off"),
              signature: str = Form("")):
    p = mailer.PRESETS.get(preset, {})
    smtp_host, smtp_port = p.get("smtp_host", smtp_host), p.get("smtp_port", smtp_port)
    smtp_security, imap_host, imap_port = p.get("smtp_security", smtp_security), p.get("imap_host", imap_host), p.get("imap_port", imap_port)
    enc = encrypt(password)
    try:
        ex("INSERT INTO inboxes(email, from_name, smtp_host, smtp_port, smtp_security, smtp_user, smtp_pass, imap_host, "
           "imap_port, imap_user, imap_pass, daily_limit, ramp_days, ramp_started, warmup_enabled, signature, created_at) "
           "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
           (email.strip().lower(), from_name, smtp_host, smtp_port, smtp_security, email.strip(), enc, imap_host,
            imap_port, email.strip(), enc, daily_limit, ramp_days, now_iso(), 1 if warmup_enabled == "on" else 0,
            signature, now_iso()))
    except Exception as e:
        return back("/inboxes", f"Could not add inbox: {e}", "err")
    return back("/inboxes", f"Inbox {email} added. Click 'Test' to check the connection.")


@app.post("/inboxes/{inbox_id}/test")
def test_inbox(inbox_id: int):
    inbox = q1("SELECT * FROM inboxes WHERE id=?", (inbox_id,))
    problems = mailer.test_connection(inbox)
    if problems:
        ex("UPDATE inboxes SET last_error=? WHERE id=?", ("; ".join(problems)[:500], inbox_id))
        return back("/inboxes", " | ".join(problems), "err")
    ex("UPDATE inboxes SET last_error='', status=CASE WHEN status='error' THEN 'active' ELSE status END WHERE id=?", (inbox_id,))
    return back("/inboxes", f"{inbox['email']}: SMTP and IMAP connected")


@app.post("/inboxes/{inbox_id}/update")
def update_inbox(inbox_id: int, daily_limit: int = Form(...), ramp_days: int = Form(...), min_gap_seconds: int = Form(...),
                 warmup_daily: int = Form(...), from_name: str = Form(""), signature: str = Form(""),
                 warmup_enabled: str = Form("off"), password: str = Form("")):
    ex("UPDATE inboxes SET daily_limit=?, ramp_days=?, min_gap_seconds=?, warmup_daily=?, from_name=?, signature=?, "
       "warmup_enabled=? WHERE id=?", (daily_limit, ramp_days, min_gap_seconds, warmup_daily, from_name, signature,
                                     1 if warmup_enabled == "on" else 0, inbox_id))
    if password:
        enc = encrypt(password)
        ex("UPDATE inboxes SET smtp_pass=?, imap_pass=? WHERE id=?", (enc, enc, inbox_id))
    return back("/inboxes", "Inbox updated")


@app.post("/inboxes/{inbox_id}/toggle")
def toggle_inbox(inbox_id: int):
    ex("UPDATE inboxes SET status=CASE WHEN status='active' THEN 'paused' ELSE 'active' END WHERE id=?", (inbox_id,))
    return back("/inboxes")


@app.post("/inboxes/{inbox_id}/delete")
def delete_inbox(inbox_id: int):
    ex("UPDATE enrollments SET inbox_id=NULL WHERE inbox_id=? AND status='active' AND next_step=0", (inbox_id,))
    ex("DELETE FROM inboxes WHERE id=?", (inbox_id,))
    return back("/inboxes", "Inbox removed")


@app.get("/api/dns/{domain}")
def dns_check(domain: str):
    """SPF / DKIM / DMARC / MX health check for a sending domain."""
    import dns.resolver

    def txt(name):
        try:
            return [b"".join(r.strings).decode() for r in dns.resolver.resolve(name, "TXT", lifetime=8)]
        except Exception:
            return []
    from verify import mx_hosts
    spf = [t for t in txt(domain) if t.lower().startswith("v=spf1")]
    dmarc = [t for t in txt(f"_dmarc.{domain}") if t.lower().startswith("v=dmarc1")]
    dkim = {}
    for sel in ("google", "selector1", "selector2", "default", "k1", "zmail", "s1"):
        recs = [t for t in txt(f"{sel}._domainkey.{domain}") if "p=" in t]
        if recs:
            dkim[sel] = True
    return {"domain": domain, "mx": list(mx_hosts(domain)), "spf": spf, "dmarc": dmarc,
            "dkim_selectors_found": list(dkim), "ok": bool(spf and dmarc and dkim and mx_hosts(domain))}


# ================================================================ campaigns

@app.get("/campaigns", response_class=HTMLResponse)
def campaigns_page(request: Request):
    return page(request, "campaigns.html", campaigns=campaign_rows(), lists=q("SELECT * FROM lists ORDER BY id DESC"))


@app.post("/campaigns")
def new_campaign(name: str = Form(...), list_id: str = Form("")):
    cid = ex("INSERT INTO campaigns(name, list_id, created_at) VALUES (?,?,?)", (name, int(list_id) if list_id else None, now_iso()))
    for i, inbox in enumerate(q("SELECT id FROM inboxes WHERE status='active'")):
        ex("INSERT INTO campaign_inboxes VALUES (?,?)", (cid, inbox["id"]))
    sid = ex("INSERT INTO steps(campaign_id, position, delay_days) VALUES (?,0,0)", (cid,))
    ex("INSERT INTO variants(step_id, label, subject, body) VALUES (?,?,?,?)", (sid, "A", "", ""))
    return back(f"/campaigns/{cid}")


@app.get("/campaigns/{cid}", response_class=HTMLResponse)
def campaign_page(request: Request, cid: int):
    c = q1("SELECT * FROM campaigns WHERE id=?", (cid,))
    if not c:
        raise HTTPException(404)
    steps = engine.campaign_steps(cid)
    for s in steps:
        for v in s["variants"]:
            v["sent"] = q1("SELECT COUNT(*) n FROM messages WHERE variant_id=? AND kind='out'", (v["id"],))["n"]
            v["replies"] = q1("SELECT COUNT(DISTINCT m.lead_id) n FROM messages m WHERE m.kind='in' AND m.in_reply_to IN "
                              "(SELECT message_id FROM messages WHERE variant_id=? AND kind='out')", (v["id"],))["n"]
    attached = {r["inbox_id"] for r in q("SELECT inbox_id FROM campaign_inboxes WHERE campaign_id=?", (cid,))}
    sample = q1("SELECT * FROM leads WHERE list_id=? AND verify_status IN ('valid','risky') ORDER BY id LIMIT 1",
                (c["list_id"],)) if c["list_id"] else None
    previews = []
    if sample:
        inbox = q1("SELECT * FROM inboxes WHERE id IN (SELECT inbox_id FROM campaign_inboxes WHERE campaign_id=?)", (cid,)) \
            or {"email": "you@yourdomain.com", "from_name": "You", "signature": ""}
        fake_enr = {"thread_subject": ""}
        for s in steps:
            for v in s["variants"]:
                if v["body"].strip():
                    subj, body, _, _ = engine.compose(fake_enr, sample, inbox, s, v, c)
                    if not fake_enr["thread_subject"]:
                        fake_enr["thread_subject"] = subj
                    previews.append({"step": s["position"] + 1, "label": v["label"], "subject": subj, "body": body})
    counts = {r["status"]: r["n"] for r in q("SELECT status, COUNT(*) n FROM enrollments WHERE campaign_id=? GROUP BY status", (cid,))}
    eligible = 0
    if c["list_id"]:
        sts = "('valid','risky')" if c["include_risky"] else "('valid')"
        eligible = q1(f"SELECT COUNT(*) n FROM leads WHERE list_id=? AND verify_status IN {sts}", (c["list_id"],))["n"]
    import zoneinfo
    return page(request, "campaign.html", c=c, steps=steps, lists=q("SELECT * FROM lists ORDER BY id DESC"),
                inboxes=q("SELECT * FROM inboxes ORDER BY id"), attached=attached, previews=previews, sample=sample,
                counts=counts, eligible=eligible, timezones=sorted(zoneinfo.available_timezones()),
                stats=next((r for r in campaign_rows() if r["id"] == cid), {}))


@app.post("/campaigns/{cid}/settings")
async def campaign_settings(request: Request, cid: int):
    f = await request.form()
    days = ",".join(d for d in engine.DAYS if f.get(f"day_{d}"))
    ex("UPDATE campaigns SET name=?, list_id=?, timezone=?, window_start=?, window_end=?, send_days=?, daily_cap=?, "
       "stop_on_reply=?, include_risky=?, unsubscribe_footer=?, plain_text=? WHERE id=?",
       (f.get("name"), int(f["list_id"]) if f.get("list_id") else None, f.get("timezone"), f.get("window_start"),
        f.get("window_end"), days, int(f.get("daily_cap") or 100), 1 if f.get("stop_on_reply") else 0,
        1 if f.get("include_risky") else 0, 1 if f.get("unsubscribe_footer") else 0, 1 if f.get("plain_text") else 0, cid))
    ex("DELETE FROM campaign_inboxes WHERE campaign_id=?", (cid,))
    for k in f.keys():
        if k.startswith("inbox_"):
            ex("INSERT INTO campaign_inboxes VALUES (?,?)", (cid, int(k[6:])))
    return back(f"/campaigns/{cid}", "Settings saved")


@app.post("/campaigns/{cid}/sequence")
async def save_sequence(request: Request, cid: int):
    """Receives the full sequence as JSON from the editor and replaces steps/variants (keeping ids where possible)."""
    data = await request.json()
    keep_steps = set()
    for pos, s in enumerate(data.get("steps", [])):
        sid = s.get("id")
        if sid and q1("SELECT 1 FROM steps WHERE id=? AND campaign_id=?", (sid, cid)):
            ex("UPDATE steps SET position=?, delay_days=? WHERE id=?", (pos, int(s.get("delay_days") or 0), sid))
        else:
            sid = ex("INSERT INTO steps(campaign_id, position, delay_days) VALUES (?,?,?)", (cid, pos, int(s.get("delay_days") or 0)))
        keep_steps.add(sid)
        keep_vars = set()
        for v in s.get("variants", []):
            vid = v.get("id")
            if vid and q1("SELECT 1 FROM variants WHERE id=? AND step_id=?", (vid, sid)):
                ex("UPDATE variants SET label=?, subject=?, body=?, active=? WHERE id=?",
                   (v.get("label", "A"), v.get("subject", ""), v.get("body", ""), 1 if v.get("active", True) else 0, vid))
            else:
                vid = ex("INSERT INTO variants(step_id, label, subject, body, active) VALUES (?,?,?,?,?)",
                         (sid, v.get("label", "A"), v.get("subject", ""), v.get("body", ""), 1 if v.get("active", True) else 0))
            keep_vars.add(vid)
        for old in q("SELECT id FROM variants WHERE step_id=?", (sid,)):
            if old["id"] not in keep_vars and not q1("SELECT 1 FROM messages WHERE variant_id=?", (old["id"],)):
                ex("DELETE FROM variants WHERE id=?", (old["id"],))
            elif old["id"] not in keep_vars:
                ex("UPDATE variants SET active=0 WHERE id=?", (old["id"],))
    for old in q("SELECT id FROM steps WHERE campaign_id=?", (cid,)):
        if old["id"] not in keep_steps:
            ex("DELETE FROM steps WHERE id=?", (old["id"],))
    return {"ok": True}


@app.post("/campaigns/{cid}/ai-write")
async def ai_write(request: Request, cid: int):
    data = await request.json()
    seq = ai.write_sequence(data.get("offer", ""), data.get("audience", ""), data.get("tone", "friendly, direct"),
                            data.get("cta", "interest"))
    return {"steps": seq, "used_ai": bool(get_setting("anthropic_api_key"))}


@app.post("/campaigns/{cid}/launch")
def launch(cid: int):
    try:
        n = engine.launch_campaign(cid)
    except ValueError as e:
        return back(f"/campaigns/{cid}", str(e), "err")
    return back(f"/campaigns/{cid}", f"Campaign live: {n} new leads enrolled. Sending runs inside your window.")


@app.post("/campaigns/{cid}/pause")
def pause(cid: int):
    ex("UPDATE campaigns SET status=CASE WHEN status='active' THEN 'paused' ELSE 'active' END WHERE id=?", (cid,))
    return back(f"/campaigns/{cid}")


@app.post("/campaigns/{cid}/delete")
def delete_campaign(cid: int):
    ex("DELETE FROM campaigns WHERE id=?", (cid,))
    return back("/campaigns", "Campaign deleted")


# ================================================================ master inbox

@app.get("/unibox", response_class=HTMLResponse)
def unibox(request: Request, label: str = "", msg: int = 0):
    where, params = "m.kind='in'", []
    if label:
        where += " AND m.classification=?"
        params.append(label)
    items = q(f"SELECT m.*, l.first_name, l.last_name, l.company, l.title, c.name campaign_name FROM messages m "
              f"LEFT JOIN leads l ON l.id=m.lead_id LEFT JOIN campaigns c ON c.id=m.campaign_id "
              f"WHERE {where} ORDER BY m.id DESC LIMIT 300", params)
    thread, current = [], None
    if msg:
        current = q1("SELECT m.*, l.first_name, l.last_name, l.company, l.title, l.linkedin_url FROM messages m "
                     "LEFT JOIN leads l ON l.id=m.lead_id WHERE m.id=?", (msg,))
        if current:
            ex("UPDATE messages SET is_read=1 WHERE id=?", (msg,))
            thread = q("SELECT * FROM messages WHERE lead_id=? AND kind IN ('out','in') ORDER BY id", (current["lead_id"],))
    counts = {r["classification"]: r["n"] for r in q("SELECT classification, COUNT(*) n FROM messages WHERE kind='in' GROUP BY classification")}
    return page(request, "unibox.html", items=items, thread=thread, current=current, label=label, counts=counts,
                labels=ai.REPLY_LABELS)


@app.post("/unibox/{mid}/reply")
def unibox_reply(mid: int, body: str = Form(...)):
    try:
        engine.send_manual_reply(mid, body)
    except mailer.SendError as e:
        return back(f"/unibox?msg={mid}", f"Send failed: {e}", "err")
    return back(f"/unibox?msg={mid}", "Reply sent")


@app.post("/unibox/{mid}/label")
def unibox_label(mid: int, label: str = Form(...)):
    ex("UPDATE messages SET classification=? WHERE id=?", (label, mid))
    if label == "unsubscribe":
        m = q1("SELECT from_addr FROM messages WHERE id=?", (mid,))
        engine.suppress(m["from_addr"], "manual")
    return back(f"/unibox?msg={mid}")


# ================================================================ settings

@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    import os
    s = all_settings()
    env_locked = {k for k in SETTING_DEFAULTS if os.environ.get(k.upper())}
    return page(request, "settings.html", s=s, env_locked=env_locked,
                suppression=q("SELECT * FROM suppression ORDER BY created_at DESC LIMIT 500"))


@app.post("/settings")
async def save_settings(request: Request):
    f = await request.form()
    for k in SETTING_DEFAULTS:
        if k in ("smtp_probe", "send_engine_enabled", "warmup_engine_enabled"):
            set_setting(k, "1" if f.get(k) else "0")
        elif k in f and not (k.endswith("_api_key") and f[k] == "••••••••"):
            set_setting(k, str(f[k]).strip())
    return back("/settings", "Settings saved")


@app.post("/settings/test-provider")
def test_provider():
    try:
        prov = get_provider()
        res = prov.search({"titles": ["Head of Quality"], "locations": ["United Kingdom"]}, 1, 1)
    except ProviderError as e:
        return back("/settings", str(e), "err")
    return back("/settings", f"{prov.name}: connected. Test search found {res.total:,} people (no credits used).")


@app.post("/suppression")
def add_suppression(entries: str = Form(...)):
    n = 0
    for e in entries.replace(",", "\n").splitlines():
        e = e.strip().lower()
        if "@" in e:
            engine.suppress(e, "manual")
            n += 1
    return back("/settings", f"Added {n} entries to the do-not-contact list")


@app.post("/suppression/delete")
def del_suppression(email: str = Form(...)):
    ex("DELETE FROM suppression WHERE email=?", (email,))
    return back("/settings", "Removed")


# ================================================================ unsubscribe (public)

def _lead_from_token(token):
    val = unsign(token)
    if not val or not val.startswith("l"):
        return None
    return q1("SELECT * FROM leads WHERE id=?", (int(val[1:]),))


@app.get("/u/{token}", response_class=HTMLResponse)
def unsubscribe_page(request: Request, token: str):
    lead = _lead_from_token(token)
    return templates.TemplateResponse(request, "unsubscribe.html", {"lead": lead, "done": False, "token": token})


@app.post("/u/{token}", response_class=HTMLResponse)
def unsubscribe(request: Request, token: str):
    lead = _lead_from_token(token)
    if lead:
        engine.suppress(lead["email"], "unsubscribe link")
    return templates.TemplateResponse(request, "unsubscribe.html", {"lead": lead, "done": True, "token": token})


# ================================================================ developer API (see /docs)

@app.get("/api/campaigns")
def api_campaigns():
    return campaign_rows()


@app.get("/api/lists/{list_id}/leads")
def api_list_leads(list_id: int):
    return q("SELECT * FROM leads WHERE list_id=?", (list_id,))


@app.post("/api/lists/{list_id}/leads")
async def api_add_leads(request: Request, list_id: int):
    rows = await request.json()
    rows = rows if isinstance(rows, list) else [rows]
    return {"added": add_leads(list_id, [{**blank_row(), **r} for r in rows], "api")}


@app.get("/api/replies")
def api_replies(label: str = ""):
    sql = "SELECT m.id, m.created_at, m.from_addr, m.subject, m.body, m.classification, m.campaign_id FROM messages m WHERE kind='in'"
    return q(sql + (" AND classification=?" if label else "") + " ORDER BY id DESC LIMIT 500", (label,) if label else ())


@app.post("/api/engine/run")
def api_run_engine():
    """Run every engine job once now (handy for testing)."""
    return {"verified": engine.verify_tick(500), "sent": engine.send_tick(), "synced": engine.sync_tick(),
            "warmup": engine.warmup_tick()}
