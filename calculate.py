"""
calculate.py  –  per-platform pro-rating, TROAS, correct aggregations
"""
import os, json, logging, math, calendar
from datetime import date, timedelta
from collections import defaultdict
import pandas as pd
import numpy as np
import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)
os.makedirs(config.PROCESSED_DIR, exist_ok=True)

# ── helpers ────────────────────────────────────────────────────────────────
def safe_div(a, b, fb=0.0):
    try:
        if b == 0 or pd.isna(b): return fb
        return float(a) / float(b)
    except: return fb

def pct(v, d=1):
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))): return 0.0
    return round(float(v) * 100, d)

def fmt(v, d=0):
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))): return 0.0
    return round(float(v), d)

def nan_to_none(obj):
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)): return None
    if isinstance(obj, dict): return {k: nan_to_none(v) for k, v in obj.items()}
    if isinstance(obj, list): return [nan_to_none(i) for i in obj]
    return obj

def revenue_flag(att):
    if att < config.ATTAINMENT_CRITICAL: return "critical"
    if att < config.ATTAINMENT_WARNING:  return "warning"
    return "good"

def discount_flag(var):
    if var > config.DISCOUNT_CRITICAL: return "critical"
    if var > config.DISCOUNT_WARNING:  return "warning"
    return "good"

def roas_flag(mtd, lfm):
    if lfm and lfm > 0 and (lfm - mtd) / lfm > config.ROAS_DROP_WARNING: return "warning"
    return "good"

def root_cause(units_att, disc_var):
    ug = units_att < config.ATTAINMENT_WARNING
    dc = disc_var  > config.DISCOUNT_WARNING
    if ug and dc: return "Both"
    if ug:        return "Units Gap"
    if dc:        return "Discount Creep"
    return "On Track"

def target_stretch(plan, lm, l3m):
    l3m_mo = l3m / 3 if l3m else 0
    return (plan > config.TARGET_STRETCH_FACTOR * lm if lm else False) and \
           (plan > config.TARGET_STRETCH_FACTOR * l3m_mo if l3m_mo else False)

# ── per-platform prorate factor ────────────────────────────────────────────
def parse_sheet_date(raw, today):
    """
    Try both DD/MM and MM/DD interpretations of ambiguous date strings.
    Picks whichever lands in the current month.
    Also handles datetime objects and Excel serials.
    """
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None

    # Datetime object (openpyxl direct read)
    if hasattr(raw, 'year'):
        try:
            return raw.date() if hasattr(raw, 'date') else raw
        except: pass

    raw_str = str(raw).strip()
    candidates = []

    # Try both DD/MM and MM/DD
    for dayfirst in [True, False]:
        try:
            d = pd.to_datetime(raw_str, dayfirst=dayfirst).date()
            candidates.append(d)
        except: pass

    # Try Excel serial
    try:
        d = date(1899, 12, 30) + timedelta(days=int(float(raw_str)))
        candidates.append(d)
    except: pass

    # Return whichever lands in current month (latest day wins if tie)
    current = [d for d in candidates if d.month == today.month and d.year == today.year]
    if current:
        return max(current)
    return None


def build_prorate_map(sales: pd.DataFrame) -> dict:
    """Return {platform: (elapsed_day, total_days, factor)}"""
    result = {}
    col = "MTD Updated Till (Date)"
    if col not in sales.columns:
        return result

    today = date.today()

    for _, row in sales[["Platform", col]].drop_duplicates("Platform").iterrows():
        p   = row["Platform"]
        raw = row[col]
        d   = parse_sheet_date(raw, today) if pd.notna(raw) else None

        if d is None:
            log.warning(f"    {p}: could not parse {raw!r} — using today (day {today.day})")
            d = today

        total = calendar.monthrange(d.year, d.month)[1]
        result[p] = (d.day, total, d.day / total)

    log.info("  Per-platform prorate factors:")
    for p, (el, tot, f) in sorted(result.items()):
        log.info(f"    {p:16} day {el:2}/{tot}  factor={f:.4f}")
    return result
# ── load ────────────────────────────────────────────────────────────────────
def load_data():
    log.info("Loading raw CSVs…")
    sales = pd.read_csv(f"{config.RAW_DIR}/sales_data.csv")
    ads   = pd.read_csv(f"{config.RAW_DIR}/ads_data.csv")
    guide = pd.read_csv(f"{config.RAW_DIR}/category_guidelines.csv")
    for df in [sales, ads, guide]: df.columns = df.columns.str.strip()

    num_sales = ["Planned Quantity","Planned MRP Revenue","Planned SP Revenue",
                 "MTD Actual Quantity","MTD Actual MRP Revenue","MTD Actual SP Revenue",
                 "Last Month Units","Last Month SP Revenue","Last 3month Units","Last 3month SP Revenue"]
    num_ads   = ["Gross Clicks","Gross Units","Gross Sales","Ad Spend",
                 "Ad Impressions","Ad Clicks","Ad Units","Ad Sales"]
    for c in num_sales:
        if c in sales.columns: sales[c] = pd.to_numeric(sales[c], errors="coerce").fillna(0)
    for c in num_ads:
        if c in ads.columns: ads[c] = pd.to_numeric(ads[c], errors="coerce").fillna(0)
    guide["MRP"]           = pd.to_numeric(guide["MRP"],           errors="coerce").fillna(0)
    guide["Selling Price"] = pd.to_numeric(guide["Selling Price"], errors="coerce").fillna(0)

    # ── Platform value normalization ────────────────────────────────
    # Fix: sales & ads CSVs are independent sources; case/whitespace drift in
    # the Platform column caused chips to split (sales-side "Blinkit" vs
    # ads-side " Blinkit "/"blinkit") and made ROAS/TROAS look unfiltered on
    # the Revenue page. Strip both, then remap ads → canonical sales names.
    sales["Platform"] = sales["Platform"].astype(str).str.strip()
    ads["Platform"]   = ads["Platform"].astype(str).str.strip()

    canonical = {p.lower(): p for p in sales["Platform"].unique() if p and p.lower() != "nan"}
    unmapped  = set()
    def _remap(p):
        key = p.lower()
        if key in canonical:
            return canonical[key]
        unmapped.add(p)
        return p
    ads["Platform"] = ads["Platform"].apply(_remap)

    sales_plats = set(sales["Platform"].unique())
    ads_plats   = set(ads["Platform"].unique())
    log.info(f"  Platforms — sales:{sorted(sales_plats)}")
    log.info(f"  Platforms — ads  :{sorted(ads_plats)}")
    if unmapped:
        log.warning(f"  ⚠ Platforms in ads NOT found in sales (left as-is): {sorted(unmapped)}")
    sales_only = sales_plats - ads_plats
    if sales_only:
        log.warning(f"  ⚠ Platforms in sales NOT found in ads (ROAS will be 0 when filtered to these): {sorted(sales_only)}")
    # ────────────────────────────────────────────────────────────────

    log.info(f"  Sales:{len(sales):,}  Ads:{len(ads):,}  Guide:{len(guide):,}")
    return sales, ads, guide

# ── sales calculations ──────────────────────────────────────────────────────
def calc_sales(sales: pd.DataFrame, guide: pd.DataFrame, prorate_map: dict) -> pd.DataFrame:
    log.info("Calculating sales metrics…")

    # per-platform prorate
    sales["_pf"]  = sales["Platform"].map(lambda p: prorate_map.get(p, (8, 30, 8/30))[2])
    sales["_day"] = sales["Platform"].map(lambda p: prorate_map.get(p, (8, 30, 8/30))[0])
    sales["_tot"] = sales["Platform"].map(lambda p: prorate_map.get(p, (8, 30, 8/30))[1])
    sales["Prorated Planned SP Revenue"]  = sales["Planned SP Revenue"]  * sales["_pf"]
    sales["Prorated Planned MRP Revenue"] = sales["Planned MRP Revenue"] * sales["_pf"]
    sales["Prorated Planned Quantity"]    = sales["Planned Quantity"]    * sales["_pf"]

    # guideline join
    gd = guide.drop_duplicates("SKU Code", keep="last").set_index("SKU Code")[["MRP","Selling Price"]].to_dict("index")
    sales["Guideline MRP"] = sales["SKU"].map(lambda s: gd.get(s, {}).get("MRP", np.nan))
    sales["Guideline SP"]  = sales["SKU"].map(lambda s: gd.get(s, {}).get("Selling Price", np.nan))
    sales["Guideline Discount %"] = sales.apply(lambda r: safe_div(r["Guideline MRP"]-r["Guideline SP"], r["Guideline MRP"]), axis=1)

    # discounts
    sales["Planned Discount %"] = sales.apply(
        lambda r: safe_div(r["Planned MRP Revenue"]-r["Planned SP Revenue"], r["Planned MRP Revenue"]), axis=1)
    sales["Actual Discount %"]  = sales.apply(
        lambda r: safe_div(r["MTD Actual MRP Revenue"]-r["MTD Actual SP Revenue"], r["MTD Actual MRP Revenue"]), axis=1)
    sales["Actual SP per Unit"]  = sales.apply(
        lambda r: safe_div(r["MTD Actual SP Revenue"], r["MTD Actual Quantity"]), axis=1)
    sales["Discount vs Plan Variance"]      = sales["Actual Discount %"] - sales["Planned Discount %"]
    sales["Discount vs Guideline Variance"] = sales["Actual Discount %"] - sales["Guideline Discount %"]

    # attainments vs prorated
    sales["Revenue Attainment %"] = sales.apply(
        lambda r: safe_div(r["MTD Actual SP Revenue"], r["Prorated Planned SP Revenue"]), axis=1)
    sales["Units Attainment %"]   = sales.apply(
        lambda r: safe_div(r["MTD Actual Quantity"], r["Prorated Planned Quantity"]), axis=1)
    sales["Revenue Gap"] = sales["MTD Actual SP Revenue"] - sales["Prorated Planned SP Revenue"]
    sales["Units Gap"]   = sales["MTD Actual Quantity"]   - sales["Prorated Planned Quantity"]

    # ── LM/L3M MRP revenue from Category Guidelines MRP (Tab 3) ──────
    # Use guideline MRP per unit directly (more accurate than plan-derived).
    # SKUs not in guidelines are excluded (MRP per unit = NaN → excluded from totals).
    sales["MRP per Unit"]    = sales["SKU"].map(
        lambda s: gd.get(s, {}).get("MRP", np.nan)
    )
    sales["LM MRP Revenue"]  = sales["Last Month Units"]        * sales["MRP per Unit"]
    sales["L3M MRP Revenue"] = (sales["Last 3month Units"] / 3) * sales["MRP per Unit"]
    sales["LM SP Monthly"]   = sales["Last Month SP Revenue"]
    sales["L3M SP Monthly"]  = sales["Last 3month SP Revenue"] / 3
    sales["LM Discount %"]   = sales.apply(
        lambda r: safe_div(r["LM MRP Revenue"]  - r["LM SP Monthly"],  r["LM MRP Revenue"])
        if pd.notna(r["MRP per Unit"]) else np.nan, axis=1)
    sales["L3M Discount %"]  = sales.apply(
        lambda r: safe_div(r["L3M MRP Revenue"] - r["L3M SP Monthly"], r["L3M MRP Revenue"])
        if pd.notna(r["MRP per Unit"]) else np.nan, axis=1)

    # growth (prorated LM/L3M)
    sales["LM Prorated Revenue"]  = sales["Last Month SP Revenue"]   * sales["_pf"]
    sales["L3M Prorated Revenue"] = (sales["Last 3month SP Revenue"] / 3) * sales["_pf"]
    sales["LM Revenue Growth %"]  = sales.apply(
        lambda r: safe_div(r["MTD Actual SP Revenue"]-r["LM Prorated Revenue"], r["LM Prorated Revenue"]), axis=1)
    sales["L3M Revenue Growth %"] = sales.apply(
        lambda r: safe_div(r["MTD Actual SP Revenue"]-r["L3M Prorated Revenue"], r["L3M Prorated Revenue"]), axis=1)

    # flags
    sales["Revenue Flag"]  = sales["Revenue Attainment %"].apply(revenue_flag)
    sales["Discount Flag"] = sales["Discount vs Guideline Variance"].apply(discount_flag)
    sales["Target Stretch"] = sales.apply(
        lambda r: target_stretch(r["Planned Quantity"], r["Last Month Units"], r["Last 3month Units"]), axis=1)
    sales["Root Cause"] = sales.apply(
        lambda r: root_cause(r["Units Attainment %"], r["Discount vs Guideline Variance"]), axis=1)
    return sales

# ── ads calculations ────────────────────────────────────────────────────────
def calc_ads(ads: pd.DataFrame) -> tuple:
    log.info("Calculating ads metrics…")
    ads["Time"] = ads["Time"].astype(str).str.strip()
    ads["Period"] = ads["Time"].apply(lambda t: "LFM" if t=="LFM" else "L3M" if t=="L3M" else "MTD")
    ads["CPC"]  = ads.apply(lambda r: safe_div(r["Ad Spend"], r["Ad Clicks"]),      axis=1)
    ads["CPM"]  = ads.apply(lambda r: safe_div(r["Ad Spend"], r["Ad Impressions"])*1000, axis=1)
    ads["CTR"]  = ads.apply(lambda r: safe_div(r["Ad Clicks"], r["Ad Impressions"]), axis=1)
    ads["ROAS"] = ads.apply(lambda r: safe_div(r["Ad Sales"],  r["Ad Spend"]),       axis=1)
    ads["TROAS"]= ads.apply(lambda r: safe_div(r["Gross Sales"], r["Ad Spend"]),     axis=1)
    ads["Ad Contribution %"] = ads.apply(lambda r: safe_div(r["Ad Units"], r["Gross Units"]), axis=1)
    mtd = ads[ads["Period"]=="MTD"].copy()
    lfm = ads[ads["Period"]=="LFM"].copy()
    l3m = ads[ads["Period"]=="L3M"].copy()
    return mtd, lfm, l3m

# ── aggregation ─────────────────────────────────────────────────────────────
def agg_sales_by(sales, group_cols):
    agg = sales.groupby(group_cols, as_index=False).agg(
        Planned_SP_Revenue   =("Planned SP Revenue",          "sum"),
        Planned_MRP_Revenue  =("Planned MRP Revenue",         "sum"),
        Prorated_Planned_SP  =("Prorated Planned SP Revenue", "sum"),
        Prorated_Planned_MRP =("Prorated Planned MRP Revenue","sum"),
        Prorated_Planned_Qty =("Prorated Planned Quantity",   "sum"),
        Actual_SP_Revenue    =("MTD Actual SP Revenue",       "sum"),
        Actual_MRP_Revenue   =("MTD Actual MRP Revenue",      "sum"),
        Planned_Qty          =("Planned Quantity",            "sum"),
        Actual_Qty           =("MTD Actual Quantity",         "sum"),
        LM_Revenue           =("Last Month SP Revenue",       "sum"),
        L3M_Revenue          =("Last 3month SP Revenue",      "sum"),
        LM_Prorated_Revenue  =("LM Prorated Revenue",         "sum"),
        L3M_Prorated_Revenue =("L3M Prorated Revenue",        "sum"),
        LM_MRP_Revenue       =("LM MRP Revenue",              "sum"),
        L3M_MRP_Revenue      =("L3M MRP Revenue",             "sum"),
        Target_Stretch_Count =("Target Stretch",              "sum"),
        SKU_Count            =("SKU",                         "nunique"),
    )
    agg["Revenue_Attainment"]  = agg.apply(lambda r: safe_div(r["Actual_SP_Revenue"],  r["Prorated_Planned_SP"]),  axis=1)
    agg["Units_Attainment"]    = agg.apply(lambda r: safe_div(r["Actual_Qty"],         r["Prorated_Planned_Qty"]), axis=1)
    agg["Revenue_Gap"]         = agg["Actual_SP_Revenue"] - agg["Prorated_Planned_SP"]
    agg["Units_Gap"]           = agg["Actual_Qty"]        - agg["Prorated_Planned_Qty"]
    agg["Actual_Discount_Pct"] = agg.apply(lambda r: safe_div(r["Actual_MRP_Revenue"]-r["Actual_SP_Revenue"], r["Actual_MRP_Revenue"]), axis=1)
    agg["Planned_Discount_Pct"]= agg.apply(lambda r: safe_div(r["Planned_MRP_Revenue"]-r["Planned_SP_Revenue"], r["Planned_MRP_Revenue"]), axis=1)
    agg["Discount_vs_Plan"]    = agg["Actual_Discount_Pct"] - agg["Planned_Discount_Pct"]
    agg["LM_Growth_Pct"]       = agg.apply(lambda r: safe_div(r["Actual_SP_Revenue"]-r["LM_Prorated_Revenue"], r["LM_Prorated_Revenue"]), axis=1)
    agg["L3M_Growth_Pct"]      = agg.apply(lambda r: safe_div(r["Actual_SP_Revenue"]-r["L3M_Prorated_Revenue"], r["L3M_Prorated_Revenue"]), axis=1)
    agg["Revenue_Flag"]        = agg["Revenue_Attainment"].apply(revenue_flag)
    # pct-ify
    for c in ["Revenue_Attainment","Units_Attainment","Actual_Discount_Pct","Planned_Discount_Pct",
              "Discount_vs_Plan","LM_Growth_Pct","L3M_Growth_Pct"]:
        agg[c] = agg[c].apply(lambda v: pct(v))
    return agg

def agg_ads_periods(mtd, lfm, l3m, group_cols):
    def _agg(df, sfx):
        g = df.groupby(group_cols, as_index=False).agg(
            **{f"Ad_Spend_{sfx}":       ("Ad Spend",       "sum")},
            **{f"Ad_Sales_{sfx}":       ("Ad Sales",       "sum")},
            **{f"Ad_Units_{sfx}":       ("Ad Units",       "sum")},
            **{f"Ad_Impressions_{sfx}": ("Ad Impressions", "sum")},
            **{f"Ad_Clicks_{sfx}":      ("Ad Clicks",      "sum")},
            **{f"Gross_Units_{sfx}":    ("Gross Units",    "sum")},
            **{f"Gross_Sales_{sfx}":    ("Gross Sales",    "sum")},
            **{f"Gross_Clicks_{sfx}":   ("Gross Clicks",   "sum")},
        )
        g[f"ROAS_{sfx}"]  = g.apply(lambda r: safe_div(r[f"Ad_Sales_{sfx}"],       r[f"Ad_Spend_{sfx}"]),       axis=1)
        g[f"TROAS_{sfx}"] = g.apply(lambda r: safe_div(r[f"Gross_Sales_{sfx}"],    r[f"Ad_Spend_{sfx}"]),       axis=1)
        g[f"CPC_{sfx}"]   = g.apply(lambda r: safe_div(r[f"Ad_Spend_{sfx}"],       r[f"Ad_Clicks_{sfx}"]),      axis=1)
        g[f"CPM_{sfx}"]   = g.apply(lambda r: safe_div(r[f"Ad_Spend_{sfx}"],       r[f"Ad_Impressions_{sfx}"])*1000, axis=1)
        g[f"CTR_{sfx}"]   = g.apply(lambda r: safe_div(r[f"Ad_Clicks_{sfx}"],      r[f"Ad_Impressions_{sfx}"]), axis=1)
        g[f"Ad_Contribution_{sfx}"] = g.apply(lambda r: safe_div(r[f"Ad_Units_{sfx}"], r[f"Gross_Units_{sfx}"]), axis=1)
        g[f"CPO_{sfx}"]   = g.apply(lambda r: safe_div(r[f"Ad_Spend_{sfx}"],       r[f"Ad_Units_{sfx}"]),       axis=1)
        return g
    m = _agg(mtd, "MTD"); lf = _agg(lfm, "LFM"); l3 = _agg(l3m, "L3M")
    merged = m.merge(lf, on=group_cols, how="outer").merge(l3, on=group_cols, how="outer")
    for col in merged.columns:
        if merged[col].dtype == object and col not in group_cols:
            try: merged[col] = pd.to_numeric(merged[col])
            except: pass
    merged = merged.fillna(0)
    merged["ROAS_Delta_vs_LFM"]      = merged.apply(lambda r: safe_div(r["ROAS_MTD"]-r["ROAS_LFM"],         r["ROAS_LFM"]),      axis=1)
    merged["TROAS_Delta_vs_LFM"]     = merged.apply(lambda r: safe_div(r["TROAS_MTD"]-r["TROAS_LFM"],       r["TROAS_LFM"]),     axis=1)
    merged["CPC_Delta_vs_LFM"]       = merged.apply(lambda r: safe_div(r["CPC_MTD"]-r["CPC_LFM"],           r["CPC_LFM"]),       axis=1)
    merged["Ad_Spend_Delta_vs_LFM"]  = merged.apply(lambda r: safe_div(r["Ad_Spend_MTD"]-r["Ad_Spend_LFM"], r["Ad_Spend_LFM"]), axis=1)
    merged["Ad_Units_Delta_vs_LFM"]  = merged.apply(lambda r: safe_div(r["Ad_Units_MTD"]-r["Ad_Units_LFM"], r["Ad_Units_LFM"]), axis=1)
    merged["ROAS_Flag"] = merged.apply(lambda r: roas_flag(r["ROAS_MTD"], r["ROAS_LFM"]), axis=1)
    # pct-ify rate fields
    for sfx in ["MTD","LFM","L3M"]:
        for c in [f"CTR_{sfx}", f"Ad_Contribution_{sfx}"]:
            if c in merged.columns: merged[c] = merged[c].apply(lambda v: pct(v))
    for c in ["ROAS_Delta_vs_LFM","TROAS_Delta_vs_LFM","CPC_Delta_vs_LFM",
              "Ad_Spend_Delta_vs_LFM","Ad_Units_Delta_vs_LFM"]:
        if c in merged.columns: merged[c] = merged[c].apply(lambda v: pct(v))
    return merged

# ── summary ─────────────────────────────────────────────────────────────────
def build_summary(sales, ads_mtd, ads_lfm, ads_l3m, prorate_map):
    tot_plan    = sales["Planned SP Revenue"].sum()
    tot_prorated= sales["Prorated Planned SP Revenue"].sum()
    tot_actual  = sales["MTD Actual SP Revenue"].sum()
    tot_lm      = sales["Last Month SP Revenue"].sum()
    tot_l3m     = sales["Last 3month SP Revenue"].sum()
    lm_prorated = sales["LM Prorated Revenue"].sum()
    l3m_prorated= sales["L3M Prorated Revenue"].sum()

    # actual discount
    tot_act_mrp = sales["MTD Actual MRP Revenue"].sum()
    act_disc_pct= (tot_act_mrp - tot_actual) / tot_act_mrp * 100 if tot_act_mrp else 0

    # ads totals
    sp_mtd = ads_mtd["Ad Spend"].sum();   sl_mtd = ads_mtd["Ad Sales"].sum()
    gs_mtd = ads_mtd["Gross Sales"].sum();su_mtd = ads_mtd["Ad Units"].sum()
    si_mtd = ads_mtd["Ad Impressions"].sum(); sc_mtd = ads_mtd["Ad Clicks"].sum()
    gsu_mtd= ads_mtd["Gross Units"].sum()

    sp_lfm = ads_lfm["Ad Spend"].sum();   sl_lfm = ads_lfm["Ad Sales"].sum()
    gs_lfm = ads_lfm["Gross Sales"].sum();su_lfm = ads_lfm["Ad Units"].sum()

    sp_l3m = ads_l3m["Ad Spend"].sum();   sl_l3m = ads_l3m["Ad Sales"].sum()
    gs_l3m = ads_l3m["Gross Sales"].sum();su_l3m = ads_l3m["Ad Units"].sum()

    roas_mtd  = safe_div(sl_mtd, sp_mtd);  troas_mtd = safe_div(gs_mtd, sp_mtd)
    roas_lfm  = safe_div(sl_lfm, sp_lfm);  troas_lfm = safe_div(gs_lfm, sp_lfm)
    roas_l3m  = safe_div(sl_l3m, sp_l3m);  troas_l3m = safe_div(gs_l3m, sp_l3m)
    cpo_mtd   = safe_div(sp_mtd, su_mtd);  ctr_mtd   = safe_div(sc_mtd, si_mtd)*100
    adc_mtd   = safe_div(su_mtd, gsu_mtd)*100

    crit_rev  = int((sales["Revenue Flag"]=="critical").sum())
    warn_rev  = int((sales["Revenue Flag"]=="warning").sum())
    disc_crit = int((sales["Discount Flag"]=="critical").sum())
    disc_warn = int((sales["Discount Flag"]=="warning").sum())
    disc_ok   = int((sales["Discount Flag"]=="good").sum())

    # median prorate for display
    factors = [v[2] for v in prorate_map.values()]
    med_factor = sorted(factors)[len(factors)//2] if factors else 8/30
    med_day    = round(med_factor * 30)

    return {
        "total_planned_revenue":   fmt(tot_plan),
        "total_prorated_revenue":  fmt(tot_prorated),
        "total_actual_revenue":    fmt(tot_actual),
        "total_lm_revenue":        fmt(tot_lm),
        "total_l3m_monthly":       fmt(tot_l3m / 3),
        "lm_prorated_revenue":     fmt(lm_prorated),
        "l3m_prorated_revenue":    fmt(l3m_prorated),
        "total_revenue_gap":       fmt(tot_actual - tot_prorated),
        "overall_attainment_pct":  pct(safe_div(tot_actual, tot_prorated)),
        "lm_attainment_pct":       pct(safe_div(tot_actual, lm_prorated)),
        "l3m_attainment_pct":      pct(safe_div(tot_actual, l3m_prorated)),
        "actual_discount_pct":     round(act_disc_pct, 1),
        # LM/L3M discount: sum only rows where guideline MRP is available
        "lm_discount_pct":         round(safe_div(
            (sales["LM MRP Revenue"] - sales["LM SP Monthly"]).dropna().sum(),
            sales["LM MRP Revenue"].dropna().sum()) * 100, 1),
        "l3m_discount_pct":        round(safe_div(
            (sales["L3M MRP Revenue"] - sales["L3M SP Monthly"]).dropna().sum(),
            sales["L3M MRP Revenue"].dropna().sum()) * 100, 1),
        "median_elapsed_day":      med_day,
        "total_days":              30,
        # ads MTD
        "ad_spend_mtd":   fmt(sp_mtd), "ad_sales_mtd": fmt(sl_mtd),
        "gross_sales_mtd":fmt(gs_mtd), "ad_units_mtd": fmt(su_mtd),
        "roas_mtd":       fmt(roas_mtd, 2), "troas_mtd": fmt(troas_mtd, 2),
        "cpo_mtd":        fmt(cpo_mtd), "ctr_mtd": round(ctr_mtd, 2),
        "ad_contrib_mtd": round(adc_mtd, 1),
        # ads LFM
        "ad_spend_lfm":   fmt(sp_lfm), "ad_sales_lfm": fmt(sl_lfm),
        "gross_sales_lfm":fmt(gs_lfm), "ad_units_lfm": fmt(su_lfm),
        "roas_lfm":       fmt(roas_lfm, 2), "troas_lfm": fmt(troas_lfm, 2),
        "cpo_lfm":        fmt(safe_div(sp_lfm, su_lfm)),
        # ads L3M monthly avg
        "ad_spend_l3m":   fmt(sp_l3m / 3), "ad_sales_l3m": fmt(sl_l3m / 3),
        "gross_sales_l3m":fmt(gs_l3m / 3), "ad_units_l3m": fmt(su_l3m / 3),
        "roas_l3m":       fmt(roas_l3m, 2), "troas_l3m": fmt(troas_l3m, 2),
        "cpo_l3m":        fmt(safe_div(sp_l3m, su_l3m)),
        # flags
        "critical_revenue_skus": crit_rev, "warning_revenue_skus": warn_rev,
        "disc_critical_skus": disc_crit, "disc_warning_skus": disc_warn, "disc_ok_skus": disc_ok,
    }

# ── discount audit ───────────────────────────────────────────────────────────
def build_discount_audit(sales):
    records = []
    for _, r in sales.iterrows():
        records.append({
            "Platform": r["Platform"], "Category": r["Category"],
            "SKU": r["SKU"], "Short_Name": r["Short Name"],
            "MRP": fmt(r.get("Guideline MRP", 0)),
            "Guideline_SP": fmt(r.get("Guideline SP", 0)),
            "Actual_SP_per_Unit": fmt(r.get("Actual SP per Unit", 0)),
            "Guideline_Discount_Pct": pct(r.get("Guideline Discount %", 0)),
            "Planned_Discount_Pct":   pct(r.get("Planned Discount %", 0)),
            "Actual_Discount_Pct":    pct(r.get("Actual Discount %", 0)),
            "Discount_vs_Plan":       pct(r.get("Discount vs Plan Variance", 0)),
            "Discount_vs_Guideline":  pct(r.get("Discount vs Guideline Variance", 0)),
            "Discount_Flag":          r.get("Discount Flag", "good"),
        })
    return records

def df_to_records(df): return nan_to_none(df.to_dict(orient="records"))

# ── main ─────────────────────────────────────────────────────────────────────
def main():
    log.info("="*55)
    log.info("  calculate.py  –  per-platform prorate + TROAS")
    log.info("="*55)

    sales, ads, guide = load_data()
    prorate_map = build_prorate_map(sales)
    sales = calc_sales(sales, guide, prorate_map)
    
    # ── Verification log ──────────────────────────────────────────
    tot_pro = sales['Prorated Planned SP Revenue'].sum()
    tot_act = sales['MTD Actual SP Revenue'].sum()
    tot_plan= sales['Planned SP Revenue'].sum()
    log.info(f'  Verification:')
    log.info(f'    Monthly Plan      : ₹{tot_plan/1e7:.2f} Cr')
    log.info(f'    Prorated Target   : ₹{tot_pro/1e7:.2f} Cr  (should be ~₹3.00 Cr)')
    log.info(f'    MTD Actual        : ₹{tot_act/1e7:.2f} Cr  (should be ~₹2.95 Cr)')
    log.info(f'    Attainment vs Pace: {tot_act/tot_pro*100:.1f}%  (should be ~98-99%)')
    # ─────────────────────────────────────────────────────────────
    
    mtd, lfm, l3m = calc_ads(ads)

    log.info("Aggregating revenue…")
    cat_summary      = agg_sales_by(sales, ["Category"])
    cat_plat_summary = agg_sales_by(sales, ["Category", "Platform"])

    sku_detail = sales[[
        "Category","Platform","SKU","Short Name",
        "Planned Quantity","Prorated Planned Quantity","MTD Actual Quantity","Units Attainment %",
        "Planned SP Revenue","Prorated Planned SP Revenue","MTD Actual SP Revenue","Revenue Attainment %",
        "Revenue Gap","Units Gap",
        "Planned Discount %","Actual Discount %","Discount vs Plan Variance",
        "Discount vs Guideline Variance","Guideline Discount %",
        "LM Revenue Growth %","L3M Revenue Growth %",
        "Revenue Flag","Discount Flag","Root Cause","Target Stretch",
        "Last Month Units","Last 3month Units",
    ]].copy()
    for c in ["Units Attainment %","Revenue Attainment %","Planned Discount %","Actual Discount %",
              "Discount vs Plan Variance","Discount vs Guideline Variance","Guideline Discount %",
              "LM Revenue Growth %","L3M Revenue Growth %"]:
        if c in sku_detail.columns: sku_detail[c] = sku_detail[c].apply(lambda v: pct(v))

    log.info("Aggregating ads…")
    sku_cat_map = sales[["SKU","Category"]].drop_duplicates().set_index("SKU")["Category"].to_dict()
    for df in [mtd, lfm, l3m]: df["Category"] = df["SKU"].map(sku_cat_map).fillna("Unknown")

    ads_by_cat      = agg_ads_periods(mtd, lfm, l3m, ["Category"])
    ads_by_plat     = agg_ads_periods(mtd, lfm, l3m, ["Platform"])
    ads_by_cat_plat = agg_ads_periods(mtd, lfm, l3m, ["Category","Platform"])
    ads_sku_detail  = agg_ads_periods(mtd, lfm, l3m, ["Category","Platform","SKU"])

    summary = build_summary(sales, mtd, lfm, l3m, prorate_map)
    log.info(f"  Total prorated: {summary['total_prorated_revenue']/1e7:.2f} Cr  "
             f"Actual: {summary['total_actual_revenue']/1e7:.2f} Cr  "
             f"Attainment: {summary['overall_attainment_pct']}%")

    # discount
    discount_sku = build_discount_audit(sales)
    discount_by_cat = []
    for cat, grp in sales.groupby("Category"):
        discount_by_cat.append({
            "Category": cat,
            "Planned_Revenue": fmt(grp["Planned SP Revenue"].sum()),
            "Guideline_Discount_Pct": pct(safe_div((grp["Guideline MRP"]-grp["Guideline SP"]).sum(), grp["Guideline MRP"].sum())),
            "Planned_Discount_Pct":   pct(safe_div((grp["Planned MRP Revenue"]-grp["Planned SP Revenue"]).sum(), grp["Planned MRP Revenue"].sum())),
            "Actual_Discount_Pct":    pct(safe_div((grp["MTD Actual MRP Revenue"]-grp["MTD Actual SP Revenue"]).sum(), grp["MTD Actual MRP Revenue"].sum())),
            "Discount_vs_Plan":       pct(grp["Discount vs Plan Variance"].mean()),
            "Discount_vs_Guideline":  pct(grp["Discount vs Guideline Variance"].mean()),
            "Discount_Flag":          discount_flag(grp["Discount vs Guideline Variance"].mean()),
            "SKU_Count":              int(grp["SKU"].nunique()),
        })
    discount_by_cat_plat = []
    for (cat, plat), grp in sales.groupby(["Category","Platform"]):
        discount_by_cat_plat.append({
            "Category": cat, "Platform": plat,
            "Guideline_Discount_Pct": pct(safe_div((grp["Guideline MRP"]-grp["Guideline SP"]).sum(), grp["Guideline MRP"].sum())),
            "Planned_Discount_Pct":   pct(safe_div((grp["Planned MRP Revenue"]-grp["Planned SP Revenue"]).sum(), grp["Planned MRP Revenue"].sum())),
            "Actual_Discount_Pct":    pct(safe_div((grp["MTD Actual MRP Revenue"]-grp["MTD Actual SP Revenue"]).sum(), grp["MTD Actual MRP Revenue"].sum())),
            "Discount_vs_Guideline":  pct(grp["Discount vs Guideline Variance"].mean()),
            "Discount_Flag":          discount_flag(grp["Discount vs Guideline Variance"].mean()),
        })

    try:
        with open(f"{config.RAW_DIR}/meta.json") as f: meta = json.load(f)
    except:
        from datetime import datetime
        meta = {"fetched_at": datetime.now().strftime("%d %b %Y, %I:%M %p")}

    # per-platform prorate info for dashboard
    prorate_info = {p: {"day": v[0], "total": v[1], "factor": round(v[2], 4)}
                    for p, v in prorate_map.items()}

    def save(name, data):
        path = f"{config.PROCESSED_DIR}/{name}.json"
        with open(path, "w") as f: json.dump(nan_to_none(data), f)
        log.info(f"  → {path}")

    save("summary",              summary)
    save("meta",                 meta)
    save("prorate_info",         prorate_info)
    save("cat_summary",          df_to_records(cat_summary))
    save("cat_plat_summary",     df_to_records(cat_plat_summary))
    save("sku_detail",           df_to_records(sku_detail))
    save("ads_by_cat",           df_to_records(ads_by_cat))
    save("ads_by_plat",          df_to_records(ads_by_plat))
    save("ads_by_cat_plat",      df_to_records(ads_by_cat_plat))
    save("ads_sku_detail",       df_to_records(ads_sku_detail))
    save("discount_by_cat",      discount_by_cat)
    save("discount_by_cat_plat", discount_by_cat_plat)
    save("discount_sku",         discount_sku)
    log.info("\n✅  Done.")

if __name__ == "__main__":
    main()
