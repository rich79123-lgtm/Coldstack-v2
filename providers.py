from db import get_setting
# ---- base ----
"""Lead data provider interface.

search() returns preview rows (cheap or free). reveal() turns chosen preview rows into contacts with
emails (this is usually where a provider charges credits). Every row is a dict with these keys:
  provider_id, first_name, last_name, title, company, company_domain, industry, company_size,
  location, linkedin_url, email, provider_status
"""
from dataclasses import dataclass, field

FIELDS = ["provider_id", "first_name", "last_name", "title", "company", "company_domain", "industry",
          "company_size", "location", "linkedin_url", "email", "provider_status"]


def blank_row(**kw) -> dict:
    row = {k: "" for k in FIELDS}
    row.update({k: ("" if v is None else str(v)) for k, v in kw.items() if k in FIELDS})
    return row


@dataclass
class SearchResult:
    rows: list = field(default_factory=list)
    total: int = 0
    page: int = 1
    note: str = ""


class Provider:
    name = "base"
    charges_on_reveal = False

    def search(self, filters: dict, page: int = 1, per_page: int = 25) -> SearchResult:
        raise NotImplementedError

    def reveal(self, rows: list) -> list:
        return rows


class ProviderError(Exception):
    pass


# ---- demo ----
"""Synthetic demo data so the whole app can be tried without a paid data provider.
Emails use the reserved .example TLD, so they can never reach a real person."""
import hashlib
import random



FIRST = ["James", "Olivia", "Liam", "Amelia", "Noah", "Isla", "Oliver", "Ava", "Harry", "Mia", "Jack", "Sofia",
         "Lars", "Freja", "Erik", "Ingrid", "Sean", "Aoife", "Priya", "Arjun", "Fatima", "Omar", "Chloe", "Daniel"]
LAST = ["Smith", "Jones", "Taylor", "Brown", "Williams", "Murphy", "Kelly", "Hansen", "Nielsen", "Larsen",
        "Andersson", "Patel", "Shah", "Khan", "Walker", "Wright", "Evans", "Roberts", "O'Brien", "Lindqvist"]
PREFIX = ["Nova", "Blue", "North", "Bright", "Apex", "Clear", "Summit", "Harbor", "Pioneer", "Vertex", "Ember",
          "Granite", "Atlas", "Orbit", "Cedar", "Lumen", "Quill", "Fjord", "Silver", "Kestrel"]
SUFFIX = ["Labs", "Health", "Logistics", "Pharma", "Analytics", "Partners", "Systems", "Digital", "Works",
          "Bio", "Software", "Group", "Studio", "Capital", "Manufacturing"]
DEFAULT_TITLES = ["Head of Sales", "Founder", "Marketing Director", "VP Operations", "CEO", "Head of Growth"]
DEFAULT_LOCS = ["London, United Kingdom", "Manchester, United Kingdom", "Dublin, Ireland", "Stockholm, Sweden",
                "Copenhagen, Denmark", "New York, United States"]
SIZE = {"1,10": "1-10", "11,50": "11-50", "51,200": "51-200", "201,500": "201-500", "501,1000": "501-1000",
        "1001,5000": "1001-5000", "5001,10000": "5001-10000", "10001,": "10001+"}


class DemoProvider(Provider):
    name = "demo"

    def search(self, filters, page=1, per_page=25):
        seed = int(hashlib.md5(repr(sorted((k, tuple(v) if isinstance(v, list) else v)
                                           for k, v in filters.items())).encode()).hexdigest(), 16)
        total = 180 + seed % 900
        rnd = random.Random(seed + page)
        titles = filters.get("titles") or DEFAULT_TITLES
        locs = filters.get("locations") or DEFAULT_LOCS
        sizes = [SIZE.get(r, r) for r in (filters.get("employee_ranges") or ["11,50", "51,200"])]
        inds = filters.get("industries") or ["software", "healthcare", "logistics", "professional services"]
        rows = []
        for i in range(min(per_page, max(0, total - (page - 1) * per_page))):
            fn, ln = rnd.choice(FIRST), rnd.choice(LAST)
            co = f"{rnd.choice(PREFIX)} {rnd.choice(SUFFIX)}"
            domain = co.lower().replace(" ", "") + ".example"
            local = f"{fn}.{ln}".lower().replace("'", "")
            rows.append(blank_row(
                provider_id=f"demo-{seed % 10**8}-{page}-{i}", first_name=fn, last_name=ln,
                title=rnd.choice(titles), company=co, company_domain=domain, industry=rnd.choice(inds),
                company_size=rnd.choice(sizes), location=rnd.choice(locs),
                linkedin_url=f"https://www.linkedin.com/in/{local.replace('.', '-')}-demo",
                email=f"{local}@{domain}", provider_status="demo"))
        return SearchResult(rows=rows, total=total, page=page,
                            note="Demo data: synthetic contacts on .example domains. Add an Apollo or PDL key in Settings for real leads.")


# ---- apollo ----
"""Apollo.io adapter.

- Search: POST /api/v1/mixed_people/api_search (no credits, needs a *master* API key, returns no emails)
- Reveal: POST /api/v1/people/bulk_match (up to 10 per call, consumes credits, returns email + email_status)
Docs: https://docs.apollo.io/reference/people-api-search and /reference/bulk-people-enrichment
"""
import httpx



BASE = "https://api.apollo.io/api/v1"


class ApolloProvider(Provider):
    name = "apollo"
    charges_on_reveal = True

    def __init__(self, api_key: str):
        if not api_key:
            raise ProviderError("Apollo API key missing — add it in Settings.")
        self.headers = {"X-Api-Key": api_key, "Content-Type": "application/json", "Cache-Control": "no-cache"}

    def _post(self, path, params=None, body=None):
        """Apollo takes filters/options as query parameters (arrays use the `key[]` form) and
        only bulk_match's `details` list in the JSON body."""
        flat = []
        for k, v in (params or {}).items():
            if isinstance(v, list):
                flat += [(f"{k}[]", str(x)) for x in v]
            elif isinstance(v, bool):
                flat.append((k, "true" if v else "false"))
            else:
                flat.append((k, str(v)))
        try:
            r = httpx.post(f"{BASE}{path}", params=flat, json=body or {}, headers=self.headers, timeout=60)
        except httpx.HTTPError as e:
            raise ProviderError(f"Apollo request failed: {e}")
        if r.status_code in (401, 403):
            raise ProviderError(f"Apollo {r.status_code}: check that the key is a MASTER API key and your plan "
                                f"includes API access. {r.text[:200]}")
        if r.status_code >= 400:
            raise ProviderError(f"Apollo {r.status_code}: {r.text[:300]}")
        return r.json()

    def search(self, filters, page=1, per_page=25):
        params = {"page": page, "per_page": min(per_page, 100)}
        if filters.get("titles"):
            params["person_titles"] = filters["titles"]
            params["include_similar_titles"] = True
        if filters.get("seniorities"):
            params["person_seniorities"] = filters["seniorities"]
        if filters.get("locations"):
            params["person_locations"] = filters["locations"]
        if filters.get("employee_ranges"):
            params["organization_num_employees_ranges"] = filters["employee_ranges"]
        kw = (filters.get("industries") or []) + (filters.get("keywords") or [])
        if kw:
            params["q_organization_keyword_tags"] = kw
        if filters.get("email_statuses"):
            params["contact_email_status"] = filters["email_statuses"]
        data = self._post("/mixed_people/api_search", params)
        rows = []
        for p in data.get("people", []) or []:
            org = p.get("organization") or {}
            # Search returns only a masked last name and no location/LinkedIn; reveal() fills these in.
            rows.append(blank_row(
                provider_id=p.get("id"), first_name=p.get("first_name"),
                last_name=p.get("last_name") or p.get("last_name_obfuscated"), title=p.get("title"),
                company=org.get("name"), provider_status="has email" if p.get("has_email") else "no email"))
        total = data.get("total_entries") or len(rows)
        return SearchResult(rows=rows, total=int(total), page=page,
                            note="Apollo search is free. Saving reveals emails (about 1 credit each) and skips people Apollo has no email for.")

    def reveal(self, rows):
        rows = [r for r in rows if r.get("provider_status") != "no email"]  # don't spend lookups on known blanks
        out = []
        for i in range(0, len(rows), 10):
            chunk = rows[i:i + 10]
            data = self._post("/people/bulk_match",
                              params={"reveal_personal_emails": False, "reveal_phone_number": False},
                              body={"details": [{"id": r["provider_id"]} for r in chunk]})
            by_id = {m.get("id"): m for m in (data.get("matches") or []) if m}
            for r in chunk:
                m = by_id.get(r["provider_id"])
                if not m or not m.get("email") or m.get("email_status") == "unavailable":
                    continue
                org = m.get("organization") or {}
                out.append(blank_row(
                    provider_id=m.get("id"), first_name=m.get("first_name") or r["first_name"],
                    last_name=m.get("last_name") or r["last_name"], title=m.get("title") or r["title"],
                    company=org.get("name") or r["company"],
                    company_domain=org.get("primary_domain") or "",
                    industry=org.get("industry") or "",
                    company_size=str(org.get("estimated_num_employees") or ""),
                    location=", ".join(x for x in [m.get("city"), m.get("country")] if x),
                    linkedin_url=m.get("linkedin_url") or "", email=m.get("email"),
                    provider_status=m.get("email_status") or ""))
        return out


# ---- pdl ----
"""People Data Labs adapter.

Person Search API: POST https://api.peopledatalabs.com/v5/person/search with header X-Api-Key and an
Elasticsearch-style query. Each returned record costs one credit, so search results already include
emails and reveal() is a no-op. Check field names against PDL's current schema if a filter returns nothing.
"""
import httpx



URL = "https://api.peopledatalabs.com/v5/person/search"
SENIORITY_MAP = {"owner": "owner", "founder": "owner", "c_suite": "cxo", "partner": "partner", "vp": "vp",
                 "head": "director", "director": "director", "manager": "manager", "senior": "senior",
                 "entry": "entry"}
SIZE_MAP = {"1,10": "1-10", "11,50": "11-50", "51,200": "51-200", "201,500": "201-500", "501,1000": "501-1000",
            "1001,5000": "1001-5000", "5001,10000": "5001-10000", "10001,": "10001+"}


class PDLProvider(Provider):
    name = "pdl"

    def __init__(self, api_key: str):
        if not api_key:
            raise ProviderError("People Data Labs API key missing — add it in Settings.")
        self.headers = {"X-Api-Key": api_key, "Content-Type": "application/json"}
        self._scroll = {}

    def _query(self, f):
        must = [{"exists": {"field": "work_email"}}]
        if f.get("titles"):
            must.append({"bool": {"should": [{"match_phrase": {"job_title": t.lower()}} for t in f["titles"]]}})
        if f.get("seniorities"):
            must.append({"terms": {"job_title_levels": sorted({SENIORITY_MAP[s] for s in f["seniorities"] if s in SENIORITY_MAP})}})
        if f.get("locations"):
            must.append({"bool": {"should": [
                {"match_phrase": {"location_name": loc.lower()}} for loc in f["locations"]] + [
                {"term": {"location_country": loc.lower()}} for loc in f["locations"]]}})
        if f.get("employee_ranges"):
            must.append({"terms": {"job_company_size": [SIZE_MAP[r] for r in f["employee_ranges"] if r in SIZE_MAP]}})
        if f.get("industries"):
            must.append({"bool": {"should": [{"match": {"job_company_industry": i.lower()}} for i in f["industries"]]}})
        return {"bool": {"must": must}}

    def search(self, filters, page=1, per_page=25):
        payload = {"query": self._query(filters), "size": min(per_page, 100), "titlecase": True}
        if page > 1 and self._scroll.get(page):
            payload["scroll_token"] = self._scroll[page]
        try:
            r = httpx.post(URL, json=payload, headers=self.headers, timeout=60)
        except httpx.HTTPError as e:
            raise ProviderError(f"PDL request failed: {e}")
        if r.status_code == 404:
            return SearchResult(rows=[], total=0, page=page, note="No PDL matches.")
        if r.status_code >= 400:
            raise ProviderError(f"PDL {r.status_code}: {r.text[:300]}")
        data = r.json()
        if data.get("scroll_token"):
            self._scroll[page + 1] = data["scroll_token"]
        rows = []
        for p in data.get("data", []):
            rows.append(blank_row(
                provider_id=p.get("id"), first_name=p.get("first_name"), last_name=p.get("last_name"),
                title=p.get("job_title"), company=p.get("job_company_name"),
                company_domain=p.get("job_company_website"), industry=p.get("job_company_industry"),
                company_size=p.get("job_company_size"), location=p.get("location_name"),
                linkedin_url=p.get("linkedin_url"), email=p.get("work_email"), provider_status="pdl"))
        return SearchResult(rows=rows, total=int(data.get("total") or len(rows)), page=page,
                            note="PDL charges 1 credit per record returned, including previews.")


# ---- factory ----




def get_provider(name: str | None = None):
    name = name or get_setting("lead_provider") or "demo"
    if name == "apollo":
        return ApolloProvider(get_setting("apollo_api_key"))
    if name == "pdl":
        return PDLProvider(get_setting("pdl_api_key"))
    return DemoProvider()
