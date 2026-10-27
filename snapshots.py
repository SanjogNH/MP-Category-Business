"""
snapshots.py
────────────
Keeps a small daily copy of every platform's month-to-date numbers so the WBR
agent can work out any 7-day window: week = MTD(end day) − MTD(day before start),
matched to each platform's own "MTD Updated Till" day.

  python snapshots.py save       # save today's snapshot from data/processed/ (run after calculate.py)
  python snapshots.py backfill   # one-time: rebuild past snapshots from the repo's dashboard history

Snapshot file: agent_state/snapshots/YYYY-MM-DD.json.gz  (run date, IST)
  {"date": ..., "months": {"2026-10": {"Amazon": {"till": 23, "skus": {SKU: [cat, rev, qty, mrp_rev,
     plan_sp_month, ad_spend, ad_sales, ad_clicks, gross_sales]}}}}}
"""

import os
import sys
import json
import gzip
import glob
import logging
import calendar
import subprocess
import datetime as dt

import config

log = logging.getLogger(__name__)
SNAP_DIR = "agent_state/snapshots"
KEEP_DAYS = 75
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
F = ["cat", "rev", "qty", "mrp", "plan", "ad_spend", "ad_sales", "ad_clicks", "gross_sales"]


# ── Build a snapshot from one month's artifacts ──────────────
def month_snapshot(m):
    out = {}
    till = {p: v.get("day") for p, v in (m.get("prorate_info") or {}).items()}
    mrp = {(r["Platform"], r["SKU"]): r.get("Actual_MRP_Revenue") or 0 for r in m.get("discount_sku") or []}
    ads = {(r["Platform"], r["SKU"]): r for r in m.get("ads_sku_detail") or []}
    seen = set()
    for r in m.get("sku_detail") or []:
        k = (r["Platform"], r["SKU"])
        seen.add(k)
        a = ads.get(k, {})
        plat = out.setdefault(r["Platform"], {"till": till.get(r["Platform"]), "skus": {}})
        plat["skus"][r["SKU"]] = [r["Category"], r.get("MTD Actual SP Revenue") or 0, r.get("MTD Actual Quantity") or 0,
                                  mrp.get(k, 0), r.get("Planned SP Revenue") or 0, a.get("Ad_Spend_MTD") or 0,
                                  a.get("Ad_Sales_MTD") or 0, a.get("Ad_Clicks_MTD") or 0, a.get("Gross_Sales_MTD") or 0]
    for k, a in ads.items():  # ad-only SKUs (no sales row)
        if k in seen or a.get("Category") == "Unmapped Category":
            continue
        plat = out.setdefault(k[0], {"till": till.get(k[0]), "skus": {}})
        plat["skus"][k[1]] = [a["Category"], 0, 0, 0, 0, a.get("Ad_Spend_MTD") or 0, a.get("Ad_Sales_MTD") or 0,
                              a.get("Ad_Clicks_MTD") or 0, a.get("Gross_Sales_MTD") or 0]
    return out


def snapshot_from_payload(payload, date):
    months = payload.get("months") or list(payload.get("by_month", {}).keys())
    keep = months[-2:]  # latest month + previous one (its final close)
    return {"date": date.isoformat(),
            "months": {mk: month_snapshot(payload["by_month"][mk]) for mk in keep if mk in payload["by_month"]}}


def write(snap):
    os.makedirs(SNAP_DIR, exist_ok=True)
    with gzip.open(f"{SNAP_DIR}/{snap['date']}.json.gz", "wt", encoding="utf-8") as f:
        json.dump(snap, f, separators=(",", ":"))
    cutoff = (dt.date.fromisoformat(snap["date"]) - dt.timedelta(days=KEEP_DAYS)).isoformat()
    for p in glob.glob(f"{SNAP_DIR}/*.json.gz"):
        if os.path.basename(p)[:10] < cutoff:
            os.remove(p)


def load_processed():
    """Current data from data/processed/ in the same shape as the dashboard payload."""
    with open(f"{config.PROCESSED_DIR}/index.json") as f:
        idx = json.load(f)
    by_month = {}
    for mk in idx["months"][-2:]:
        d = {}
        for n in ("prorate_info", "sku_detail", "ads_sku_detail", "discount_sku"):
            try:
                with open(f"{config.PROCESSED_DIR}/{mk}/{n}.json") as f:
                    d[n] = json.load(f)
            except FileNotFoundError:
                d[n] = []
        by_month[mk] = d
    return {"months": idx["months"], "by_month": by_month}


def save_today():
    today = dt.datetime.now(IST).date()
    snap = snapshot_from_payload(load_processed(), today)
    write(snap)
    log.info(f"Snapshot saved for {today} ({', '.join(snap['months'])})")
    return snap


# ── Backfill from the repo's own history of nh_category_intel.html ──
def extract_payload(html_text):
    marker = "const PAYLOAD="
    i = html_text.index(marker) + len(marker)
    obj, _ = json.JSONDecoder().raw_decode(html_text, i)
    return obj


def backfill(days=45):
    out = subprocess.run(["git", "log", f"--since={days}.days", "--format=%H %cI", "--", "nh_category_intel.html"],
                         capture_output=True, text=True, check=True).stdout.split("\n")
    last_per_day = {}
    for line in out:
        if not line.strip():
            continue
        sha, ts = line.split(" ", 1)
        day = dt.datetime.fromisoformat(ts.strip()).astimezone(IST).date()
        last_per_day.setdefault(day, sha)  # git log is newest first → first seen is the day's last commit
    made = 0
    for day, sha in sorted(last_per_day.items()):
        if os.path.exists(f"{SNAP_DIR}/{day.isoformat()}.json.gz"):
            continue
        try:
            html = subprocess.run(["git", "show", f"{sha}:nh_category_intel.html"], capture_output=True,
                                  check=True).stdout.decode("utf-8", "replace")
            write(snapshot_from_payload(extract_payload(html), day))
            made += 1
        except Exception as e:
            log.warning(f"  backfill {day}: skipped ({e})")
    log.info(f"Backfill: {made} snapshots created from {len(last_per_day)} days of history")
    return made


# ── Reading snapshots & weekly math ─────────────────────────
class History:
    def __init__(self, extra_current=None):
        self.points = {}   # (month, platform) -> {till_day: {sku: row}}
        self.final = {}    # month -> {platform: {sku: row}} from the newest snapshot holding that month
        files = sorted(glob.glob(f"{SNAP_DIR}/*.json.gz"))
        snaps = []
        for p in files:
            with gzip.open(p, "rt", encoding="utf-8") as f:
                snaps.append(json.load(f))
        if extra_current:
            snaps.append(extra_current)
        for s in snaps:  # oldest → newest, newer overwrites
            for mk, plats in s["months"].items():
                for p, v in plats.items():
                    if v.get("till"):
                        self.points.setdefault((mk, p), {})[int(v["till"])] = v["skus"]
                    self.final.setdefault(mk, {})[p] = v["skus"]

    def latest_till(self, platform, month):
        pts = self.points.get((month, platform)) or {}
        return max(pts) if pts else None

    def _at(self, month, platform, day):
        """MTD rows as of `day` (latest snapshot with till ≤ day). Returns (till, rows) or (0, {})."""
        if day <= 0:
            return 0, {}
        pts = self.points.get((month, platform)) or {}
        ok = [t for t in pts if t <= day]
        if not ok:
            return None, None
        t = max(ok)
        return t, pts[t]

    def window(self, platform, start, end):
        """Per-SKU totals for [start, end] (dates).
        Returns (rows, effective_start, effective_end, days_covered, plan_for_days). If a snapshot for the exact
        start/end day is missing, the nearest earlier one is used and the effective dates say what was covered."""
        mk = lambda d: d.strftime("%Y-%m")
        total, plan, covered, eff_end, eff_start = {}, {}, 0, None, None
        d = start
        segments = []
        while d <= end:  # split the window at month boundaries
            last = dt.date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])
            segments.append((d, min(end, last)))
            d = min(end, last) + dt.timedelta(days=1)
        for a, b in segments:
            m = mk(a)
            ndays = calendar.monthrange(a.year, a.month)[1]
            t_end, rows_end = self._at(m, platform, b.day)
            if b.day == ndays and m in self.final and platform in self.final[m] and \
                    (self.latest_till(platform, m) or 0) >= b.day:
                rows_end, t_end = self.final[m][platform], b.day
            t0, rows0 = self._at(m, platform, a.day - 1)
            if rows_end is None or t_end is None or t_end < a.day or rows0 is None:
                continue  # no data for this segment
            days = t_end - t0
            if days <= 0:
                continue
            for sku, r in rows_end.items():
                r0 = rows0.get(sku)
                vals = [r[i] - (r0[i] if r0 else 0) for i in range(1, len(F))]
                cur = total.setdefault(sku, [r[0]] + [0] * (len(F) - 1))
                for i, v in enumerate(vals, 1):
                    cur[i] += v
                plan[sku] = plan.get(sku, 0) + (r[4] or 0) * days / ndays
            covered += days
            if eff_start is None:
                eff_start = dt.date(a.year, a.month, t0 + 1)
            eff_end = dt.date(a.year, a.month, t_end)
        return total, eff_start, eff_end, covered, plan


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "save"
    if cmd == "backfill":
        backfill(int(sys.argv[2]) if len(sys.argv) > 2 else 45)
    else:
        save_today()
