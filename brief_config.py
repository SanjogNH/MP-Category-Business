# ─────────────────────────────────────────────
#  brief_config.py  –  Morning brief settings
#  Edit this file to change recipients, how many
#  tasks each tab shows, and how patient the agent is.
# ─────────────────────────────────────────────

# ── Recipients ─────────────────────────────────
# Platform names must match the data exactly (see prorate_info in the JSON).
# A person listed in several teams gets ONE combined email.
# "REST" = every platform not assigned to any other team (new platforms land here automatically).
MASTER = {
    "name": "All platforms (master)",
    "emails": ["sanjog@nathabit.in"],
}

TEAMS = [
    {"name": "Quick commerce", "emails": ["garima@nathabit.in", "arman@nathabit.in"],
     "platforms": ["Blinkit", "Zepto", "Big Basket"]},
    {"name": "Instamart", "emails": ["mayank.gangwani@nathabit.in"],
     "platforms": ["Instamart"]},
    {"name": "Amazon", "emails": ["ketan@nathabit.in", "poushali@nathabit.in"],
     "platforms": ["Amazon"]},
    {"name": "Flipkart & Meesho", "emails": ["anika@nathabit.in", "pranay.sharma@nathabit.in"],
     "platforms": ["Flipkart", "Flipkart Minutes", "Meesho"]},
    {"name": "Myntra", "emails": ["anika@nathabit.in", "shtaakshi@nathabit.in"],
     "platforms": ["Myntra"]},
    {"name": "Nykaa", "emails": ["kush.sutaria@nathabit.in"],
     "platforms": ["Nykaa"]},
    {"name": "FirstCry & Cred", "emails": ["harsh.kalani@nathabit.in"],
     "platforms": ["First Cry", "Cred"]},
    {"name": "Other accounts", "emails": ["shtaakshi@nathabit.in"],
     "platforms": ["REST"]},
]

# Platforms that appear ONLY in the master brief (never in a team email, never in "REST")
MASTER_ONLY = ["D2C"]

# ── Tasks per tab ──────────────────────────────
MAX_TASKS_PER_TAB = 5          # managers see at most this many new/follow-up tasks per tab
MASTER_MAX_PER_TAB = 10        # business-wide top list in the master brief
MAX_WAITING_SHOWN = 5          # "Give it time" items shown per tab

# ── Patience ───────────────────────────────────
# Once a task is raised, the agent waits this many days before raising it again,
# unless it gets clearly worse in the meantime.
COOLDOWN_DAYS = {"revenue": 3, "ads": 3, "discount": 3}

# "Clearly worse" = escalate early, before the cool-down ends
ESCALATE_ATTAINMENT_DROP_PTS = 10    # revenue attainment fell 10+ points since raised
ESCALATE_ROAS_DROP_PCT = 15          # ROAS fell a further 15%+ since raised
ESCALATE_DISCOUNT_RISE_PP = 2        # discount vs guideline rose 2+ pp since raised

# ── Noise floors (skip rows too small to matter) ──
MIN_PRORATED_PLAN = 10_000     # revenue tasks: SKU must have ≥ ₹10k prorated plan
MIN_AD_SPEND_MTD = 10_000      # ads tasks: SKU must have ≥ ₹10k ad spend this month
MIN_DISCOUNT_LEAKAGE = 2_000   # discount tasks: ≥ ₹2k given away beyond guideline

# ── Claude ─────────────────────────────────────
MODEL = "claude-sonnet-5-5"
STATE_FILE = "agent_state/brief_state.json"
OUTPUT_DIR = "brief_output"    # email previews are written here on every run
