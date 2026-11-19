"""Runtime configuration. Environment variables override values saved in the Settings page."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("COLDSTACK_DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = Path(os.environ.get("COLDSTACK_DB", DATA_DIR / "coldstack.db"))

# Settings that can be edited in the UI (stored in the DB) with their defaults.
# Any of them can also be forced via an environment variable of the same name in upper case.
SETTING_DEFAULTS = {
    "anthropic_api_key": "",
    "anthropic_model": "claude-sonnet-5-5",
    "lead_provider": "demo",          # demo | apollo | pdl
    "apollo_api_key": "",
    "pdl_api_key": "",
    "millionverifier_api_key": "",
    "smtp_probe": "0",                # 1 = do an SMTP RCPT check during verification (needs outbound port 25)
    "public_base_url": (f"https://{os.environ['RAILWAY_PUBLIC_DOMAIN']}" if os.environ.get("RAILWAY_PUBLIC_DOMAIN")
                        else "http://localhost:8000"),  # used for unsubscribe links
    "sender_company": "",
    "sender_postal_address": "",      # shown in the footer; required by CAN-SPAM, good practice under PECR
    "webhook_url": "",                # POSTed JSON on every reply / bounce / unsubscribe (Zapier, Make, n8n, CRM)
    "send_engine_enabled": "1",
    "warmup_engine_enabled": "1",
}

# Optional HTTP basic-auth password for the whole UI (recommended if you deploy to a server).
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
# On a public host, refuse to serve the app until a password is set.
HOSTED = bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RENDER") or os.environ.get("REQUIRE_PASSWORD"))
