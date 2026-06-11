"""
fetch_data.py
─────────────
Pulls all three tabs from Google Sheets using the Sheets API v4
(service-account auth) and saves them as CSV files in data/raw/.

Run:  python fetch_data.py
"""

import os
import sys
import json
import csv
import logging
from datetime import datetime

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import config

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


# ── Auth ───────────────────────────────────────────────────────────────────
def get_service():
    if not os.path.exists(config.CREDENTIALS_FILE):
        log.error(
            f"Credentials file '{config.CREDENTIALS_FILE}' not found.\n"
            "Download your service-account JSON from Google Cloud Console and "
            f"save it as '{config.CREDENTIALS_FILE}' in the project root."
        )
        sys.exit(1)
    creds = Credentials.from_service_account_file(
        config.CREDENTIALS_FILE, scopes=SCOPES
    )
    return build("sheets", "v4", credentials=creds)


# ── Fetch one tab ──────────────────────────────────────────────────────────
def fetch_tab(service, sheet_id: str, tab_name: str) -> list[list]:
    """Return all rows (including header) from a named tab."""
    try:
        result = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=sheet_id, range=tab_name)
            .execute()
        )
        rows = result.get("values", [])
        log.info(f"  '{tab_name}': fetched {len(rows):,} rows (incl. header)")
        return rows
    except HttpError as e:
        log.error(f"  Google Sheets API error for tab '{tab_name}': {e}")
        sys.exit(1)


# ── Save to CSV ────────────────────────────────────────────────────────────
def save_csv(rows: list[list], filepath: str):
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerows(rows)
    log.info(f"  Saved → {filepath}")


# ── Validate schema ────────────────────────────────────────────────────────
EXPECTED_HEADERS = {
    config.TAB_SALES: [
        "Platform", "MTD Updated Till (Date)", "SKU", "Short Name", "Category",
        "Planned Quantity", "Planned MRP Revenue", "Planned SP Revenue",
        "MTD Actual Quantity", "MTD Actual MRP Revenue", "MTD Actual SP Revenue",
        "Last Month Units", "Last Month SP Revenue",
        "Last 3month Units", "Last 3month SP Revenue",
    ],
    config.TAB_ADS: [
        "Platform", "Time", "SKU", "Gross Clicks", "Gross Units",
        "Gross Sales", "Ad Spend", "Ad Impressions", "Ad Clicks",
        "Ad Units", "Ad Sales",
    ],
    config.TAB_GUIDELINES: ["SKU Code", "MRP", "Selling Price"],
}


def validate_headers(rows: list[list], tab_name: str) -> bool:
    if not rows:
        log.error(f"  '{tab_name}' is empty!")
        return False
    actual   = [str(h).strip() for h in rows[0]]
    expected = EXPECTED_HEADERS.get(tab_name, [])
    missing  = [h for h in expected if h not in actual]
    if missing:
        log.warning(f"  '{tab_name}' missing expected columns: {missing}")
        return False
    return True


# ── Main ───────────────────────────────────────────────────────────────────
def main():
    log.info("=" * 55)
    log.info("  fetch_data.py  –  pulling from Google Sheets")
    log.info("=" * 55)

    service = get_service()

    tabs = {
        config.TAB_SALES:      f"{config.RAW_DIR}/sales_data.csv",
        config.TAB_ADS:        f"{config.RAW_DIR}/ads_data.csv",
        config.TAB_GUIDELINES: f"{config.RAW_DIR}/category_guidelines.csv",
    }

    all_ok = True
    for tab_name, filepath in tabs.items():
        log.info(f"Fetching '{tab_name}'…")
        rows = fetch_tab(service, config.SHEET_ID, tab_name)
        if not validate_headers(rows, tab_name):
            all_ok = False
        save_csv(rows, filepath)

    # Write a fetch-timestamp so the dashboard can show when data was last refreshed
    meta = {"fetched_at": datetime.now().strftime("%d %b %Y, %I:%M %p")}
    meta_path = f"{config.RAW_DIR}/meta.json"
    with open(meta_path, "w") as f:
        json.dump(meta, f)
    log.info(f"  Metadata saved → {meta_path}")

    log.info("")
    if all_ok:
        log.info("✅  All tabs fetched successfully.")
    else:
        log.warning("⚠️   Fetch completed with warnings — check logs above.")


if __name__ == "__main__":
    main()
