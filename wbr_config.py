# ─────────────────────────────────────────────
#  wbr_config.py  –  Weekly Business Review agent settings
# ─────────────────────────────────────────────

SENDER_NAME = "Sanjog"            # queries go out from GMAIL_USER under this name
HEAD_EMAIL = "sanjog@nathabit.in" # receives the WBR pack

# One entry per WBR. "to" gets the questions, "cc" are co-owners (any of them can reply).
# Platform names must match the data. "REST" = every platform not listed elsewhere here (and not D2C).
MANAGERS = [
    {"key": "harsh", "name": "Harsh", "to": ["harsh.kalani@nathabit.in"], "cc": [],
     "platforms": ["First Cry", "Cred"], "day": "Mon", "time": "12:30"},
    {"key": "garima", "name": "Garima", "to": ["garima.garg@nathabit.in"], "cc": ["arman@nathabit.in"],
     "platforms": ["Blinkit", "Zepto", "Big Basket"], "day": "Mon", "time": "15:00"},
    {"key": "kush", "name": "Kush", "to": ["kush.sutaria@nathabit.in"], "cc": [],
     "platforms": ["Nykaa"], "day": "Mon", "time": "16:00"},
    {"key": "mayank", "name": "Mayank", "to": ["mayank.gangwani@nathabit.in"], "cc": [],
     "platforms": ["Instamart"], "day": "Mon", "time": "17:00"},
    {"key": "anika", "name": "Anika", "to": ["anika@nathabit.in"],
     "cc": ["pranay.sharma@nathabit.in", "shtaakshi@nathabit.in"],
     "platforms": ["Flipkart", "Flipkart Minutes", "Meesho", "Myntra", "REST"], "day": "Tue", "time": "12:15"},
    {"key": "ketan", "name": "Ketan", "to": ["ketan@nathabit.in"], "cc": ["poushali@nathabit.in"],
     "platforms": ["Amazon"], "day": "Tue", "time": "15:00"},
]
NEVER_IN_WBR = ["D2C"]            # outside purview

# Working Saturdays: 1st, 3rd and 5th of the month — the agent works this out by itself every week.
WORKING_SATURDAYS = {1, 3, 5}
# One-off exceptions only (dates as "YYYY-MM-DD"). Leave empty in a normal week.
EXTRA_WORKING_SATURDAYS = []      # e.g. ["2026-11-14"] if the 2nd Saturday is made a working day
SATURDAYS_OFF = []                # e.g. ["2026-11-21"] if the 3rd Saturday is declared a holiday

# Timings (the schedule itself lives in .github/workflows/wbr.yml)
QUERY_DAYS_BEFORE = 4             # Thu for Monday WBRs, Fri for Tuesday WBRs
REPLY_DEADLINE = "6 pm"           # on the working day before the WBR

# Questions
MAX_QUESTIONS = 6
REVENUE_QUESTIONS = 3             # biggest weekly gaps (category × platform)
MIN_WEEK_TARGET = 25_000          # ignore category × platform rows with a smaller weekly target
MIN_WEEK_AD_SPEND = 25_000        # ads question only above this weekly spend
ROAS_DROP_FOR_QUESTION = 20       # % drop in ROAS week-on-week
DISCOUNT_PP_FOR_QUESTION = 5      # pp above guideline for the week

# Business snapshot table (managers fill it in and reply): Target and Estimated achievement for the month
SNAPSHOT_METRICS = ["Primary", "Secondary", "Discounting %", "Spend", "SLA"]
SNAPSHOT_UNITS = {"Primary": "₹ L", "Secondary": "₹ L", "Discounting %": "%", "Spend": "₹ L", "SLA": "%",
                  "Availability": "%"}
QUICK_COMMERCE = ["Blinkit", "Zepto", "Big Basket", "Instamart", "Flipkart Minutes"]   # SLA → Availability
OTHER_ACCOUNTS_LABEL = "Other accounts"   # the "REST" platforms share one table
FLAG_ESTIMATE_GAP_PCT = 10                # flag an estimate more than 10% away from the dashboard's projection
MIN_DAYS_FOR_PROJECTION = 5               # don't project month-end from fewer days of data

MODEL = "claude-sonnet-5-5"
STATE_DIR = "agent_state/wbr"
OUTPUT_DIR = "wbr_output"
