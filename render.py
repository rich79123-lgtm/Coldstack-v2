"""Message rendering: spintax, merge tags with fallbacks, footer, plain-text -> HTML."""
import hashlib
import html
import random
import re

SPIN_RE = re.compile(r"\{([^{}]*\|[^{}]*)\}")
TAG_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*(?:\|\s*([^}]*?))?\s*\}\}")


def spin(text: str, rnd: random.Random) -> str:
    # Resolve innermost {a|b|c} groups first so nesting works. Merge tags use {{ }} and are left alone.
    prev = None
    while prev != text:
        prev = text
        text = SPIN_RE.sub(lambda m: rnd.choice(m.group(1).split("|")), _protect(text))
        text = _unprotect(text)
    return text


def _protect(t):
    return t.replace("{{", "\x00").replace("}}", "\x01")


def _unprotect(t):
    return t.replace("\x00", "{{").replace("\x01", "}}")


def merge(text: str, data: dict) -> str:
    def rep(m):
        val = str(data.get(m.group(1).lower(), "") or "").strip()
        return val if val else (m.group(2) or "")
    return TAG_RE.sub(rep, text)


def lead_vars(lead: dict, inbox: dict | None = None) -> dict:
    import json
    v = {k: lead.get(k, "") for k in ("email", "first_name", "last_name", "title", "company", "company_domain",
                                      "industry", "location")}
    v["full_name"] = f"{lead.get('first_name', '')} {lead.get('last_name', '')}".strip()
    try:
        v.update({str(k).lower(): str(val) for k, val in json.loads(lead.get("custom") or "{}").items()})
    except Exception:
        pass
    if inbox:
        v["sender_name"] = inbox.get("from_name", "")
        v["sender_first_name"] = (inbox.get("from_name") or "").split(" ")[0]
    return v


def render(template: str, lead: dict, inbox: dict | None = None, seed: str = "") -> str:
    rnd = random.Random(int(hashlib.md5(f"{seed}{lead.get('id', '')}".encode()).hexdigest(), 16))
    return merge(spin(template or "", rnd), lead_vars(lead, inbox)).strip()


def footer(unsub_url: str, company: str, address: str) -> str:
    bits = [b for b in [company, address] if b]
    line = " · ".join(bits)
    return (f"\n\n--\n{line}\n" if line else "\n\n--\n") + f"Not relevant? Unsubscribe: {unsub_url}"


def to_html(text: str) -> str:
    esc = html.escape(text)
    esc = re.sub(r"(https?://[^\s<]+)", r'<a href="\1">\1</a>', esc)
    paras = esc.split("\n\n")
    return "".join(f"<p>{p.replace(chr(10), '<br>')}</p>" for p in paras)
