# ─────────────────────────────────────────────
#  config.py  –  Central configuration
#  Edit this file to change sheet IDs, tab
#  names, thresholds and output paths.
# ─────────────────────────────────────────────

# ── Google Sheets ──────────────────────────────
SHEET_ID = "1_dPrpqk6_8Izmick38zE_d9v_QpsebF0GzyY6WVRYrg"   # <-- your sheet ID

TAB_SALES      = "Sales Data"
TAB_ADS        = "Ads Data"
TAB_GUIDELINES = "Category Guidelines"

# Path to your Google service-account credentials JSON
# Download from Google Cloud Console → IAM → Service Accounts
CREDENTIALS_FILE = "credentials.json"

# ── Paths ──────────────────────────────────────
RAW_DIR       = "data/raw"
PROCESSED_DIR = "data/processed"
OUTPUT_HTML   = "dashboard.html"
TEMPLATE_FILE = "templates/dashboard_template.html"

# ── Flag Thresholds ────────────────────────────
# Revenue / Units attainment
ATTAINMENT_CRITICAL = 0.70   # below 70% → 🔴
ATTAINMENT_WARNING  = 0.85   # below 85% → 🟡

# Discount deviation vs guideline
DISCOUNT_CRITICAL = 0.05     # >+5 pp above guideline → 🔴
DISCOUNT_WARNING  = 0.03     # +3–5 pp above guideline → 🟡

# Target reasonableness: flag if plan > LM and L3M run-rate by this factor
TARGET_STRETCH_FACTOR = 1.30  # 130%

# Ads: ROAS drop threshold vs LFM
ROAS_DROP_WARNING = 0.20      # >20% drop → 🟡

# ── Display ────────────────────────────────────
CURRENCY_SYMBOL = "₹"
DECIMAL_PLACES  = 1
