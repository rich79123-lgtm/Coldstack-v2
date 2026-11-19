"""SMTP sending and IMAP reading for connected inboxes (Google Workspace, Microsoft 365, Zoho, any SMTP host)."""
import email
import imaplib
import smtplib
import ssl
import uuid
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr

from security import decrypt

PRESETS = {
    "google": {"smtp_host": "smtp.gmail.com", "smtp_port": 465, "smtp_security": "ssl",
               "imap_host": "imap.gmail.com", "imap_port": 993},
    "microsoft": {"smtp_host": "smtp.office365.com", "smtp_port": 587, "smtp_security": "starttls",
                  "imap_host": "outlook.office365.com", "imap_port": 993},
    "zoho": {"smtp_host": "smtp.zoho.eu", "smtp_port": 465, "smtp_security": "ssl",
             "imap_host": "imap.zoho.eu", "imap_port": 993},
}


class SendError(Exception):
    def __init__(self, msg, hard_bounce=False):
        super().__init__(msg)
        self.hard_bounce = hard_bounce


def build_message(inbox, to_addr, subject, text, html=None, in_reply_to="", references="",
                  unsubscribe_url="", extra_headers=None) -> EmailMessage:
    msg = EmailMessage()
    domain = inbox["email"].split("@")[1]
    msg["Message-ID"] = make_msgid(idstring=uuid.uuid4().hex[:12], domain=domain)
    msg["From"] = formataddr((inbox.get("from_name") or "", inbox["email"]))
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = (references + " " + in_reply_to).strip()
    if unsubscribe_url:
        msg["List-Unsubscribe"] = f"<mailto:{inbox['email']}?subject=unsubscribe>, <{unsubscribe_url}>"
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    for k, v in (extra_headers or {}).items():
        msg[k] = v
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    return msg


def smtp_send(inbox: dict, msg: EmailMessage):
    host, port, sec = inbox["smtp_host"], int(inbox["smtp_port"] or 587), inbox.get("smtp_security") or "starttls"
    user, pwd = inbox.get("smtp_user") or inbox["email"], decrypt(inbox.get("smtp_pass") or "")
    ctx = ssl.create_default_context()
    try:
        if sec == "ssl":
            server = smtplib.SMTP_SSL(host, port, timeout=30, context=ctx)
        else:
            server = smtplib.SMTP(host, port, timeout=30)
        with server as s:
            s.ehlo()
            if sec == "starttls":
                s.starttls(context=ctx)
                s.ehlo()
            if pwd and s.has_extn("auth"):
                s.login(user, pwd)
            s.send_message(msg)
    except smtplib.SMTPRecipientsRefused as e:
        raise SendError(f"recipient refused: {e.recipients}", hard_bounce=True)
    except smtplib.SMTPAuthenticationError as e:
        raise SendError(f"authentication failed: {e.smtp_error!r}")
    except (smtplib.SMTPException, OSError) as e:
        raise SendError(f"{type(e).__name__}: {e}")


def test_connection(inbox: dict) -> list[str]:
    """Returns a list of problems (empty = OK)."""
    problems = []
    try:
        sec = inbox.get("smtp_security") or "starttls"
        ctx = ssl.create_default_context()
        s = (smtplib.SMTP_SSL(inbox["smtp_host"], int(inbox["smtp_port"]), timeout=20, context=ctx) if sec == "ssl"
             else smtplib.SMTP(inbox["smtp_host"], int(inbox["smtp_port"]), timeout=20))
        with s:
            s.ehlo()
            if sec == "starttls":
                s.starttls(context=ctx)
                s.ehlo()
            pwd = decrypt(inbox.get("smtp_pass") or "")
            if pwd and s.has_extn("auth"):
                s.login(inbox.get("smtp_user") or inbox["email"], pwd)
    except Exception as e:
        problems.append(f"SMTP: {e}")
    if inbox.get("imap_host"):
        try:
            with imap_connect(inbox) as m:
                m.select("INBOX", readonly=True)
        except Exception as e:
            problems.append(f"IMAP: {e}")
    return problems


class imap_connect:
    def __init__(self, inbox):
        self.inbox = inbox

    def __enter__(self):
        self.m = imaplib.IMAP4_SSL(self.inbox["imap_host"], int(self.inbox.get("imap_port") or 993), timeout=30)
        self.m.login(self.inbox.get("imap_user") or self.inbox["email"], decrypt(self.inbox.get("imap_pass") or ""))
        return self.m

    def __exit__(self, *a):
        try:
            self.m.logout()
        except Exception:
            pass


def fetch_new(inbox: dict, last_uid: int, folder="INBOX", limit=200):
    """Yields (uid, email.message.Message). On first run (last_uid == -1) only records the current max UID."""
    with imap_connect(inbox) as m:
        typ, _ = m.select(folder, readonly=True)
        if typ != "OK":
            return [], last_uid
        typ, data = m.uid("search", None, "ALL")
        uids = [int(x) for x in (data[0] or b"").split()] if typ == "OK" else []
        if last_uid < 0:
            return [], (max(uids) if uids else 0)
        new = [u for u in uids if u > last_uid][:limit]
        out = []
        for u in new:
            typ, msg_data = m.uid("fetch", str(u), "(BODY.PEEK[])")
            if typ == "OK" and msg_data and isinstance(msg_data[0], tuple):
                out.append((u, email.message_from_bytes(msg_data[0][1])))
        return out, (max(new) if new else last_uid)


def rescue_from_spam(inbox: dict, header="X-Coldstack-Warmup") -> int:
    """Move warmup emails that landed in spam back to the inbox (helps teach the provider they're wanted)."""
    moved = 0
    for folder in ("[Gmail]/Spam", "Junk", "Junk Email", "Spam"):
        try:
            with imap_connect(inbox) as m:
                typ, _ = m.select(f'"{folder}"')
                if typ != "OK":
                    continue
                typ, data = m.uid("search", None, f'HEADER {header} ""')
                for u in (data[0] or b"").split():
                    if m.uid("copy", u, "INBOX")[0] == "OK":
                        m.uid("store", u, "+FLAGS", "(\\Deleted)")
                        moved += 1
                m.expunge()
        except Exception:
            continue
    return moved


def text_of(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition", "")):
                try:
                    return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
                except Exception:
                    continue
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                import re
                raw = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
                return re.sub(r"<[^>]+>", " ", raw)
        return ""
    try:
        return msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", "replace")
    except Exception:
        return str(msg.get_payload())


def strip_quoted(text: str) -> str:
    import re
    lines = []
    for line in text.splitlines():
        if re.match(r"^\s*>", line) or re.match(r"^On .+wrote:\s*$", line) or line.strip().startswith("-----Original Message"):
            break
        if re.match(r"^From: .+", line) and lines:
            break
        lines.append(line)
    return "\n".join(lines).strip()


def addr(header_val) -> str:
    return parseaddr(header_val or "")[1].lower()
