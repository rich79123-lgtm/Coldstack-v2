# Coldstack: a self-hosted cold email platform

Coldstack is an all-in-one cold email platform in the style of ListKit, built for running your own outreach. It covers the full workflow: find leads → verify them → send from rotated, ramped inboxes → handle replies.

| ListKit module | Coldstack equivalent |
|---|---|
| B2B lead database + plain-English AI search | **Find leads**: describe your ICP; Claude turns it into filters, which run against **Apollo** (free search, credits only when you reveal emails), **People Data Labs**, or built-in demo data. CSV import too. |
| Triple-verification | Syntax/disposable/role checks → DNS MX → mailbox check (MillionVerifier API, or a direct SMTP probe with catch-all detection). Only `valid` leads are emailed by default. |
| Domains & inboxes, warmup, ramp | Connect Google Workspace / Microsoft 365 / Zoho / any SMTP+IMAP inbox. Each inbox ramps from 5/day to its limit over N days, enforces a daily cap and a randomised gap between sends, and warms up by emailing your other inboxes (rescuing those emails from spam). Built-in SPF/DKIM/DMARC checker. |
| Email engine / sequencer | Multi-step sequences, A/B variants with per-variant reply stats, spintax, merge tags with fallbacks, custom CSV fields, send windows and days in any timezone, inbox rotation (sticky per lead so threads stay together), follow-ups threaded as replies, stop-on-reply, campaign daily cap, plain-text mode. |
| Master inbox | Replies from all inboxes in one place, auto-labelled (interested / not interested / out of office / unsubscribe / bounce). Reply in-thread from the same inbox. |
| AI script writer | Writes a 3-step sequence with A/B variants from your offer and audience (Claude; falls back to templates with no key). |
| Compliance | Signed unsubscribe link + RFC 8058 one-click `List-Unsubscribe` headers, do-not-contact list (emails or whole `@domains`), automatic bounce suppression, no double-enrolment across campaigns. |
| API & webhooks | REST API with interactive docs at `/docs`; webhook POST on every reply, bounce and unsubscribe. |

**Not included compared with ListKit:** its own 977M-contact database (you rent data from Apollo or PDL instead), automatic purchase of domains and inboxes (you buy them yourself, see below), a large shared warmup network (warmup only uses your own inboxes), and multi-user billing. Open tracking is left out on purpose because tracking pixels hurt deliverability.

## Run it

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python run.py
```
Open http://localhost:8000. It starts with demo data, so you can click through everything before spending money. Run the tests with `pytest -q`.

## Going live (rough monthly cost for ~1,000 emails/day)

1. **Domains (~£8–10 each per year):** buy 3–5 look-alike domains (e.g. `tryacme.com`, `getacme.co`). Never send cold email from your main domain.
2. **Inboxes (~£5–6 each per month):** 2–3 Google Workspace or Microsoft 365 mailboxes per domain, so 10–20 inboxes at 30–50 emails per day each. Set up SPF, DKIM and DMARC, and check them on the Inboxes page.
3. **App passwords:** for Google, turn on 2-step verification and then create an app password. For Microsoft 365, enable Authenticated SMTP. Add each inbox in the app and click **Test**.
4. **Warmup:** leave the 14-day ramp on. Don't skip it.
5. **Leads:** an Apollo plan with API access (master API key) or People Data Labs. Optionally add MillionVerifier (about $0.0004 per check) for real mailbox verification.
6. **AI:** an Anthropic API key. Costs pennies per sequence.
7. **Hosting:** deploy on any small VPS (Hetzner, DigitalOcean, Railway, Render). Set:
   ```
   HOST=0.0.0.0  APP_PASSWORD=<choose one>  SECRET_KEY=<long random string>
   PUBLIC_BASE_URL=https://outreach.yourdomain.com
   ```
   `APP_PASSWORD` puts the whole UI behind a login, except unsubscribe pages. `SECRET_KEY` encrypts stored inbox passwords. Back up the `data/` folder.

Any setting can also be supplied as an environment variable, e.g. `ANTHROPIC_API_KEY`, `APOLLO_API_KEY`, `LEAD_PROVIDER=apollo`.

## How sending works
Background workers run inside the web process:
- **Verification:** every 5 seconds.
- **Sending:** every 20 seconds, at most one email per inbox per tick. Sending respects the window, ramp, daily caps and send gap.
- **Reply and bounce sync:** IMAP, every 2 minutes. Only new mail is read and nothing is marked as read.
- **Warmup:** every 5 minutes.

`POST /api/engine/run` runs all four once on demand.

## Legal note (UK/EU)
B2B cold email to corporate addresses is generally allowed under UK PECR, provided you identify yourself and offer an easy opt-out. That changes if you email sole traders or partnerships, which are treated like individuals and need consent. GDPR still applies to the personal data you process: document a legitimate-interest assessment and honour objections at once. The US (CAN-SPAM) requires a postal address in every email. This is not legal advice, so check the rules for each country you email.

## Project layout
```
app/main.py          routes + JSON API
app/engine.py        sending, reply/bounce sync, warmup, verification queue
app/providers/       apollo.py, pdl.py, demo.py (add your own by subclassing Provider)
app/verify.py        triple verification
app/ai.py            Claude: ICP parsing, sequence writer, reply classifier (+ offline fallbacks)
app/mailer.py        SMTP/IMAP
app/templates/       UI
```
