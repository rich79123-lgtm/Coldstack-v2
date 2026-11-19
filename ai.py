"""Claude-powered features, each with a no-API-key fallback so the app still works offline.

- parse_icp: plain-English ideal customer description -> structured search filters
- write_sequence: offer + audience -> 3-step cold email sequence with A/B variants
- classify_reply: inbound reply -> interested | not_interested | out_of_office | unsubscribe | bounce | other
"""
import json
import re

from db import get_setting, log_event

SENIORITIES = ["owner", "founder", "c_suite", "partner", "vp", "head", "director", "manager", "senior", "entry"]
EMPLOYEE_RANGES = ["1,10", "11,50", "51,200", "201,500", "501,1000", "1001,5000", "5001,10000", "10001,"]
REPLY_LABELS = ["interested", "not_interested", "out_of_office", "unsubscribe", "bounce", "other"]


def _client():
    key = get_setting("anthropic_api_key")
    if not key:
        return None
    try:
        import anthropic
        return anthropic.Anthropic(api_key=key)
    except Exception as e:  # pragma: no cover
        log_event("ai", f"Anthropic client error: {e}", "error")
        return None


def _ask_json(system: str, prompt: str, max_tokens=2000):
    client = _client()
    if not client:
        return None
    try:
        resp = client.messages.create(
            model=get_setting("anthropic_model"),
            max_tokens=max_tokens,
            system=system + "\nRespond with a single JSON value only. No prose, no code fences.",
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
        start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=0)
        return json.loads(text[start:])
    except Exception as e:
        log_event("ai", f"Claude call failed, using fallback: {e}", "warning")
        return None


# ---------------------------------------------------------------- ICP parsing

def parse_icp(description: str) -> dict:
    system = (
        "You convert a description of an ideal customer profile into B2B people-search filters. "
        "Return an object with keys: titles (list of job titles, include common variants), "
        f"seniorities (subset of {SENIORITIES}), locations (list like 'United Kingdom' or 'London, United Kingdom'), "
        f"employee_ranges (subset of {EMPLOYEE_RANGES}), industries (list of short industry names), "
        "keywords (list of short company keywords), summary (one sentence restating the target)."
    )
    data = _ask_json(system, description)
    if isinstance(data, dict):
        return _clean_filters(data)
    return _clean_filters(_heuristic_icp(description))


def _clean_filters(d: dict) -> dict:
    def lst(k):
        v = d.get(k) or []
        if isinstance(v, str):
            v = [x.strip() for x in v.split(",")]
        return [str(x).strip() for x in v if str(x).strip()]
    return {
        "titles": lst("titles"),
        "seniorities": [s for s in lst("seniorities") if s in SENIORITIES],
        "locations": lst("locations"),
        "employee_ranges": [r for r in lst("employee_ranges") if r in EMPLOYEE_RANGES],
        "industries": lst("industries"),
        "keywords": lst("keywords"),
        "summary": str(d.get("summary", "")),
    }


_TITLE_HINTS = {
    "ceo": ["CEO", "Chief Executive Officer"], "founder": ["Founder", "Co-Founder"],
    "cto": ["CTO", "Chief Technology Officer"], "cfo": ["CFO", "Chief Financial Officer"],
    "marketing": ["Head of Marketing", "Marketing Director", "VP Marketing"],
    "sales": ["Head of Sales", "Sales Director", "VP Sales"],
    "quality": ["Head of Quality", "QA Director", "Quality Assurance Manager"],
    "regulatory": ["Head of Regulatory Affairs", "Regulatory Affairs Director"],
    "operations": ["COO", "Head of Operations", "Operations Director"],
    "hr": ["Head of People", "HR Director"], "engineering": ["VP Engineering", "Head of Engineering"],
    "owner": ["Owner", "Managing Director"],
}
_LOC_HINTS = {
    "uk": "United Kingdom", "united kingdom": "United Kingdom", "london": "London, United Kingdom",
    "ireland": "Ireland", "us": "United States", "usa": "United States", "united states": "United States",
    "germany": "Germany", "france": "France", "nordics": "Sweden", "sweden": "Sweden", "denmark": "Denmark",
    "norway": "Norway", "finland": "Finland", "canada": "Canada", "australia": "Australia",
}


def _heuristic_icp(text: str) -> dict:
    t = text.lower()
    titles, locs, sen, ranges, inds = [], [], [], [], []
    for k, v in _TITLE_HINTS.items():
        if re.search(rf"\b{k}s?\b", t):
            titles += v
    for k, v in _LOC_HINTS.items():
        if re.search(rf"\b{re.escape(k)}\b", t) and v not in locs:
            locs.append(v)
            if k == "nordics":
                locs += ["Denmark", "Norway", "Finland"]
    for s in ["founder", "director", "vp", "head", "manager", "owner"]:
        if s in t:
            sen.append(s)
    if re.search(r"c-?level|c-suite|chief", t):
        sen.append("c_suite")
    m = re.search(r"(\d+)\s*[-–to]+\s*(\d+)\s*(employees|staff|people)", t)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        for r in EMPLOYEE_RANGES:
            a, _, b = r.partition(",")
            a, b = int(a), int(b or 10**9)
            if b >= lo and a <= hi:
                ranges.append(r)
    for ind in ["saas", "software", "pharma", "biotech", "agency", "ecommerce", "fintech", "manufacturing",
                "construction", "logistics", "healthcare", "real estate", "recruitment", "legal", "accounting"]:
        if ind in t:
            inds.append(ind)
    return {"titles": titles, "seniorities": sen, "locations": locs, "employee_ranges": ranges,
            "industries": inds, "keywords": [], "summary": text.strip()[:200]}


# ---------------------------------------------------------- sequence writing

def write_sequence(offer: str, audience: str, tone: str = "friendly, direct", cta: str = "interest") -> list:
    """Returns [{delay_days, variants:[{label, subject, body}]}] — 3 steps, 2 variants for step 1."""
    system = (
        "You are an expert B2B cold email copywriter. Write a 3-step cold email sequence. Rules: "
        "under 90 words per email, plain text, no links in step 1, no images, one clear low-friction CTA, "
        "no spammy words (free, guarantee, act now), lower-case short subject lines (2-4 words), "
        "personalise with merge tags {{first_name}}, {{company}}, {{title}} and use spintax like {Hi|Hey} sparingly. "
        "Follow-ups have an empty subject (they thread as replies). Step 1 has variants A and B with different angles; "
        "steps 2 and 3 have one variant A. Do not include a signature or unsubscribe line (added automatically). "
        "Return a JSON array of steps: [{\"delay_days\": int, \"variants\": [{\"label\": \"A\", \"subject\": str, \"body\": str}]}]. "
        "Step 1 delay_days is 0; step 2 is 3; step 3 is 4."
    )
    prompt = f"Offer: {offer}\nTarget audience: {audience}\nTone: {tone}\nCTA style: {cta}"
    data = _ask_json(system, prompt, max_tokens=3000)
    if isinstance(data, list) and data:
        out = []
        for i, s in enumerate(data[:5]):
            vs = [{"label": v.get("label", "AB"[j % 2]), "subject": v.get("subject", "") if i == 0 else "",
                   "body": v.get("body", "")} for j, v in enumerate(s.get("variants", [])) if v.get("body")]
            if vs:
                out.append({"delay_days": int(s.get("delay_days", 0 if i == 0 else 3)), "variants": vs})
        if out:
            return out
    return _template_sequence(offer, audience)


def _template_sequence(offer: str, audience: str) -> list:
    offer = offer.strip().rstrip(".")
    return [
        {"delay_days": 0, "variants": [
            {"label": "A", "subject": "quick question, {{first_name}}",
             "body": "{Hi|Hey} {{first_name|there}},\n\nNoticed {{company|your team}} and thought this might be relevant.\n\n"
                     f"We help {audience.strip() or 'teams like yours'}: {offer}.\n\n"
                     "Worth a quick chat to see if it fits?"},
            {"label": "B", "subject": "{{company}} idea",
             "body": "{Hi|Hey} {{first_name|there}},\n\nMost {{title|leaders}} I speak to are juggling this right now, "
                     f"so I'll keep it short: {offer}.\n\nOpen to me sending over a 2-minute overview?"},
        ]},
        {"delay_days": 3, "variants": [
            {"label": "A", "subject": "",
             "body": "{{first_name|Hi}}, just floating this back to the top of your inbox.\n\n"
                     "If it's not a priority right now, no worries at all. Is there someone else at {{company|your company}} I should speak to?"},
        ]},
        {"delay_days": 4, "variants": [
            {"label": "A", "subject": "",
             "body": "Last note from me, {{first_name|there}}. If timing's better later in the year, just reply 'later' and I'll circle back then."},
        ]},
    ]


# ---------------------------------------------------------- reply classifier

def classify_reply(subject: str, body: str) -> str:
    rule = _heuristic_classify(subject, body)
    if rule in ("bounce", "out_of_office", "unsubscribe"):
        return rule  # deterministic cases don't need the model
    system = ("Classify this reply to a B2B cold email. Return {\"label\": one of " + json.dumps(REPLY_LABELS) +
              "}. 'interested' includes asking for info, pricing or a meeting.")
    data = _ask_json(system, f"Subject: {subject}\n\n{body[:3000]}", max_tokens=50)
    if isinstance(data, dict) and data.get("label") in REPLY_LABELS:
        return data["label"]
    return rule


def _heuristic_classify(subject: str, body: str) -> str:
    t = f"{subject}\n{body}".lower()
    if re.search(r"delivery status notification|undeliverable|mail delivery (failed|subsystem)|address not found|"
                 r"recipient address rejected|user unknown|mailbox unavailable", t):
        return "bounce"
    if re.search(r"out of (the )?office|on (annual )?leave|automatic reply|auto-?reply|away until|limited access to email", t):
        return "out_of_office"
    if re.search(r"unsubscribe|remove me|take me off|stop emailing|do not contact|don't contact|opt.?out", t):
        return "unsubscribe"
    if re.search(r"not interested|no thanks|no thank you|not a fit|not for us|we're good|we are good|pass on this", t):
        return "not_interested"
    if re.search(r"interested|tell me more|send (me )?(more )?(info|details|overview)|let's talk|lets talk|book|"
                 r"calendar|call|meeting|pricing|how much|sounds good|happy to chat|\byes\b", t):
        return "interested"
    return "other"
