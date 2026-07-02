"""
build_dashboard.py
──────────────────
Reads all processed JSON files from data/processed/ and renders
them into the HTML template, producing a self-contained dashboard.html.

Run:  python build_dashboard.py
"""

import os
import json
import logging
from pathlib import Path

import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def load_json(path: str, fallback):
    if not os.path.exists(path):
        log.warning(f"  Missing: {path}  (using fallback)")
        return fallback
    with open(path) as f:
        return json.load(f)


ARTIFACTS = [
    "summary", "meta", "prorate_info",
    "cat_summary", "cat_plat_summary", "sku_detail",
    "ads_by_cat", "ads_by_plat", "ads_by_cat_plat", "ads_sku_detail",
    "discount_by_cat", "discount_by_cat_plat", "discount_sku",
]


def load_month(month: str) -> dict:
    base = f"{config.PROCESSED_DIR}/{month}"
    return {name: load_json(f"{base}/{name}.json", {} if name in ("summary", "meta", "prorate_info") else [])
            for name in ARTIFACTS}


def build_payload() -> dict:
    """
    Assemble the month-keyed payload the dashboard expects:
        { months, latest, labels, by_month: { <month>: {13 artifacts} } }

    Backward-compatible: if there is no index.json (old single-month layout
    with flat files under data/processed/), wrap it as one month.
    """
    index_path = f"{config.PROCESSED_DIR}/index.json"
    if os.path.exists(index_path):
        with open(index_path) as f:
            index = json.load(f)
        months = index.get("months", [])
        by_month = {m: load_month(m) for m in months}
        return {
            "months":  months,
            "latest":  index.get("latest", months[-1] if months else None),
            "labels":  index.get("labels", {m: m for m in months}),
            "by_month": by_month,
        }

    # ── legacy fallback: flat files, single month ──────────────────
    log.warning("  No index.json found — falling back to legacy single-month layout.")
    flat = {name: load_json(f"{config.PROCESSED_DIR}/{name}.json",
                            {} if name in ("summary", "meta", "prorate_info") else [])
            for name in ARTIFACTS}
    m = "current"
    return {"months": [m], "latest": m, "labels": {m: "Current"}, "by_month": {m: flat}}


def main():
    log.info("=" * 55)
    log.info("  build_dashboard.py  –  assembling HTML (multi-month)")
    log.info("=" * 55)

    payload = build_payload()
    log.info(f"  Months: {', '.join(payload['months'])}  (latest = {payload['latest']})")
    for m in payload["months"]:
        md = payload["by_month"][m]
        log.info(f"    {m}: " + ", ".join(
            f"{k}={len(v) if isinstance(v, list) else 'obj'}" for k, v in md.items()
            if k in ("cat_summary", "sku_detail", "ads_sku_detail", "discount_sku")))

    # ── Read template ──────────────────────────────────────────
    template_path = config.TEMPLATE_FILE
    if not os.path.exists(template_path):
        log.error(f"Template not found: {template_path}")
        return

    with open(template_path, encoding="utf-8") as f:
        html = f.read()

    # ── Inject data ────────────────────────────────────────────
    data_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    html = html.replace("__DATA_PLACEHOLDER__", data_json)

    # ── Write output ───────────────────────────────────────────
    out_path = config.OUTPUT_HTML
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    size_kb = Path(out_path).stat().st_size / 1024
    log.info(f"\n✅  Dashboard written → {out_path}  ({size_kb:.1f} KB)")
    log.info("   Open in any browser — no server needed.")


if __name__ == "__main__":
    main()
