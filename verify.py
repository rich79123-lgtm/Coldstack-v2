"""Triple verification before anything is sent:

  1. Syntax, disposable-domain and role-address checks (local, free)
  2. DNS: the domain must publish MX records (local, free)
  3. Mailbox check: an external verifier (MillionVerifier) if a key is set, otherwise an optional
     SMTP RCPT probe with catch-all detection. Most home and cloud networks block outbound port 25,
     so the probe is off by default.

Result: valid | risky | invalid, plus a human-readable detail string.
"""
import random
import re
import smtplib
import socket
import string
from functools import lru_cache

import httpx

from db import get_setting

EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                      r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")
ROLE_PREFIXES = {"info", "sales", "admin", "support", "contact", "hello", "office", "team", "enquiries",
                 "accounts", "billing", "careers", "jobs", "hr", "marketing", "noreply", "no-reply", "help",
                 "webmaster", "postmaster", "abuse"}
DISPOSABLE = {"mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com", "temp-mail.org",
              "yopmail.com", "trashmail.com", "getnada.com", "sharklasers.com", "dispostable.com",
              "maildrop.cc", "fakeinbox.com", "throwawaymail.com", "mintemail.com", "emailondeck.com"}
FREE_MAIL = {"gmail.com", "googlemail.com", "yahoo.com", "hotmail.com", "outlook.com", "live.com", "aol.com",
             "icloud.com", "proton.me", "protonmail.com", "gmx.com", "yahoo.co.uk", "hotmail.co.uk"}


@lru_cache(maxsize=5000)
def mx_hosts(domain: str) -> tuple:
    try:
        import dns.resolver
        answers = dns.resolver.resolve(domain, "MX", lifetime=8)
        return tuple(str(r.exchange).rstrip(".") for r in sorted(answers, key=lambda r: r.preference)
                     if str(r.exchange).rstrip("."))
    except Exception:
        return ()


def _smtp_probe(email: str, mx: str) -> tuple[str, str]:
    domain = email.split("@")[1]
    fake = "".join(random.choices(string.ascii_lowercase, k=14)) + "@" + domain
    try:
        with smtplib.SMTP(mx, 25, timeout=12) as s:
            s.ehlo_or_helo_if_needed()
            s.mail("verify@" + (get_setting("public_base_url").split("//")[-1].split("/")[0].split(":")[0] or "localhost"))
            code, _ = s.rcpt(email)
            if code >= 500:
                return "invalid", f"mailbox rejected ({code})"
            fcode, _ = s.rcpt(fake)
            if fcode < 300:
                return "risky", "catch-all domain (accepts any address)"
            if code < 300:
                return "valid", "mailbox accepted"
            return "risky", f"inconclusive ({code})"
    except (socket.timeout, OSError, smtplib.SMTPException) as e:
        return "unknown", f"SMTP probe unavailable ({type(e).__name__})"


def _millionverifier(email: str) -> tuple[str, str] | None:
    key = get_setting("millionverifier_api_key")
    if not key:
        return None
    try:
        r = httpx.get("https://api.millionverifier.com/api/v3/", params={"api": key, "email": email, "timeout": 10},
                      timeout=20)
        data = r.json()
    except Exception as e:
        return "unknown", f"verifier error: {e}"
    res = (data.get("result") or "").lower()
    if res == "ok":
        return "valid", "verifier: ok"
    if res in ("invalid", "disposable"):
        return "invalid", f"verifier: {res}"
    if res == "catch_all":
        return "risky", "verifier: catch-all"
    return "risky", f"verifier: {res or 'unknown'}"


def verify_email(email: str, provider_status: str = "") -> tuple[str, str]:
    email = (email or "").strip().lower()
    # 1. syntax / list checks
    if not EMAIL_RE.match(email):
        return "invalid", "bad syntax"
    local, domain = email.split("@", 1)
    if domain.endswith(".example") and provider_status == "demo":
        return "valid", "demo data (not a real mailbox)"
    if domain in DISPOSABLE:
        return "invalid", "disposable domain"
    notes = []
    if local in ROLE_PREFIXES:
        notes.append("role address")
    if domain in FREE_MAIL:
        notes.append("free webmail")
    # 2. DNS
    mx = mx_hosts(domain)
    if not mx:
        return "invalid", "domain has no MX records"
    # 3. mailbox
    result = _millionverifier(email)
    if result is None and get_setting("smtp_probe") == "1":
        result = _smtp_probe(email, mx[0])
    if result is None or result[0] == "unknown":
        if provider_status.lower() == "verified":
            result = ("valid", "provider-verified + MX ok")
        else:
            detail = (result[1] + "; " if result else "") + "MX ok, mailbox not checked"
            result = ("risky", detail)
    status, detail = result
    if notes and status == "valid":
        status = "risky"
    return status, "; ".join([detail] + notes)
