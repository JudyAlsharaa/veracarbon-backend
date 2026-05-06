import os
from dotenv import load_dotenv

load_dotenv()

GEE_SERVICE_ACCOUNT = os.getenv("GEE_SERVICE_ACCOUNT")
GEE_KEY_FILE = os.getenv("GEE_KEY_FILE")
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "https://veracarbon.com").split(",")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# GEE computation timeouts and limits
GEE_TIMEOUT_SECONDS = int(os.getenv("GEE_TIMEOUT_SECONDS", "120"))
LEAKAGE_BUFFER_METERS = int(os.getenv("LEAKAGE_BUFFER_METERS", "10000"))
ADDITIONALITY_BUFFER_METERS = int(os.getenv("ADDITIONALITY_BUFFER_METERS", "5000"))
MAX_CLOUD_COVER_PCT = float(os.getenv("MAX_CLOUD_COVER_PCT", "20"))


def validate():
    missing = [v for v in ("GEE_SERVICE_ACCOUNT", "GEE_KEY_FILE", "ANTHROPIC_API_KEY") if not os.getenv(v)]
    if missing:
        raise EnvironmentError(f"Missing required env vars: {', '.join(missing)}")
