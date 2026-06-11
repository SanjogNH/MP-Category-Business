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


def load_json(name: str) -> dict | list:
    path = f"{config.PROCESSED_DIR}/{name}.json"
    if not os.path.exists(path):
        log.warning(f"  Missing processed file: {path}  (using empty fallback)")
        return {}
    with open(path) as f:
        return json.load(f)


def main():
    log.info("=" * 55)
    log.info("  build_dashboard.py  –  assembling HTML")
    log.info("=" * 55)

    # ── Load all processed data ────────────────────────────────
    data = {
        "summary":              load_json("summary"),
        "meta":                 load_json("meta"),
        "cat_summary":          load_json("cat_summary"),
        "cat_plat_summary":     load_json("cat_plat_summary"),
        "sku_detail":           load_json("sku_detail"),
        "ads_by_cat":           load_json("ads_by_cat"),
        "ads_by_plat":          load_json("ads_by_plat"),
        "ads_by_cat_plat":      load_json("ads_by_cat_plat"),
        "ads_sku_detail":       load_json("ads_sku_detail"),
        "discount_by_cat":      load_json("discount_by_cat"),
        "discount_by_cat_plat": load_json("discount_by_cat_plat"),
        "discount_sku":         load_json("discount_sku"),
    }

    log.info("  Loaded processed data:")
    for k, v in data.items():
        count = len(v) if isinstance(v, list) else "dict"
        log.info(f"    {k}: {count}")

    # ── Read template ──────────────────────────────────────────
    template_path = config.TEMPLATE_FILE
    if not os.path.exists(template_path):
        log.error(f"Template not found: {template_path}")
        return

    with open(template_path, encoding="utf-8") as f:
        html = f.read()

    # ── Inject data ────────────────────────────────────────────
    data_json = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
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
