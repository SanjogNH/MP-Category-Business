"""
brief.py
────────
Morning brief agent. Reads the processed dashboard data for the latest month,
picks the action items using the dashboard's own flag rules, remembers what it
raised on earlier days (so it gives managers time to act), asks Claude to word
the brief, and emails one brief per team plus a full master brief.

Python decides WHAT is flagged and every number shown; Claude only writes the
wording. If the Claude call fails, a rule-based wording is used and the emails
still go out.

Modes (env BRIEF_MODE):
  dry   – write email previews to brief_output/ only (default)
  test  – send every brief to TEST_RECIPIENT only; memory is not updated
  send  – send to the real recipients and update memory

Env: ANTHROPIC_API_KEY, GMAIL_USER, GMAIL_APP_PASSWORD, TEST_RECIPIENT (test mode)
Run:  python brief.py
"""

import os
import json
import html
import smtplib
import logging
import datetime as dt
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import config
import brief_config as bc

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
TODAY = dt.datetime.now(IST).date()
MODE = os.environ.get("BRIEF_MODE", "dry").strip().lower()
if MODE not in ("dry", "test", "send"):
    MODE = "dry"  # anything unexpected (e.g. "off", "false", blank) never sends
SEV_RANK = {"critical": 0, "warning": 1}
TAB_TITLES = {"revenue": "📊 Revenue", "ads": "📣 Ads Intelligence", "discount": "🏷️ Price & Discount"}


# ══════════════════════════════════════════════
#  Formatting
# ══════════════════════════════════════════════
def inr(x) -> str:
    """₹ in Indian style: Cr / L / plain with Indian grouping."""
    if x is None:
        return "–"
    sign = "−" if x < 0 else ""
    a = abs(x)
    if a >= 1e7:
        return f"{sign}₹{a / 1e7:.2f} Cr"
    if a >= 1e5:
        return f"{sign}₹{a / 1e5:.1f} L"
    s = f"{a:.0f}"
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts) + "," + tail
    return f"{sign}₹{s}"


def pct(x, d=1) -> str:
    return "–" if x is None else f"{x:.{d}f}%"


def spct(x, d=1) -> str:
    """Signed percentage for changes: +18.0% / −19.6%."""
    if x is None:
        return "–"
    return f"{'+' if x > 0 else '−' if x < 0 else ''}{abs(x):.{d}f}%"


def as_of_ist(fetched_at: str) -> str:
    """meta.fetched_at is written on the GitHub runner, which runs in UTC."""
    try:
        t = dt.datetime.strptime(fetched_at, "%d %b %Y, %I:%M %p") + dt.timedelta(hours=5, minutes=30)
        return t.strftime("%d %b %Y, %I:%M %p IST")
    except Exception:
        return fetched_at or "unknown"


# ══════════════════════════════════════════════
#  Data
# ══════════════════════════════════════════════
NEEDED = ["summary", "meta", "prorate_info", "cat_summary", "cat_plat_summary", "sku_detail",
          "ads_by_plat", "ads_by_cat_plat", "ads_sku_detail", "discount_by_cat", "discount_sku"]


def load_latest():
    """Returns (month, label, data) from data/processed/ (written by calculate.py)."""
    with open(f"{config.PROCESSED_DIR}/index.json") as f:
        idx = json.load(f)
    month = idx["latest"]
    data = {}
    for name in NEEDED:
        with open(f"{config.PROCESSED_DIR}/{month}/{name}.json") as f:
            data[name] = json.load(f)
    return month, idx.get("labels", {}).get(month, month), data


def team_scopes(all_platforms):
    """Resolve 'REST' and return [{name, emails, platforms:set}] per team."""
    named = {p for t in bc.TEAMS for p in t["platforms"] if p != "REST"}
    master_only = set(getattr(bc, "MASTER_ONLY", []))
    rest = set(all_platforms) - named - master_only
    teams = []
    for t in bc.TEAMS:
        plats = {p for p in t["platforms"] if p != "REST"}
        if "REST" in t["platforms"]:
            plats |= rest
        teams.append({"name": t["name"], "emails": t["emails"], "platforms": plats - master_only})
    unknown = named - set(all_platforms)
    if unknown:
        log.warning(f"  Platforms in brief_config not found in data: {sorted(unknown)}")
    return teams


# ══════════════════════════════════════════════
#  Candidate action items (all flagged rows)
# ══════════════════════════════════════════════
def revenue_candidates(d, excluded):
    items, stretch = [], []
    for r in d["sku_detail"]:
        flag = r.get("Revenue Flag")
        if flag not in SEV_RANK or r["Platform"] in excluded:
            continue
        if (r.get("Prorated Planned SP Revenue") or 0) < bc.MIN_PRORATED_PLAN:
            continue  # unplanned / tiny-plan SKUs: 0% attainment is noise, not a miss
        it = {
            "key": f"revenue|{r['Platform']}|{r['SKU']}",
            "tab": "revenue", "sev": flag, "platform": r["Platform"], "category": r["Category"],
            "sku": r["SKU"], "name": r.get("Short Name") or r["SKU"],
            "metric": r["Revenue Attainment %"], "root_cause": r.get("Root Cause"),
            "rank": -(r["Revenue Gap"] or 0),
            "facts": {
                "attainment_vs_pace": pct(r["Revenue Attainment %"]),
                "units_attainment": pct(r["Units Attainment %"]),
                "revenue_gap": inr(r["Revenue Gap"]),
                "mtd_revenue": inr(r["MTD Actual SP Revenue"]),
                "pace_target": inr(r["Prorated Planned SP Revenue"]),
                "root_cause": r.get("Root Cause"),
                "actual_discount": pct(r.get("Actual Discount %")),
                "guideline_discount": pct(r.get("Guideline Discount %")),
                "vs_last_month": spct(r.get("LM Revenue Growth %")),
            },
        }
        (stretch if r.get("Target Stretch") else items).append(it)
    # critical first, "Both" first within severity, then biggest rupee gap
    # severity first, then biggest rupee gap (the dashboard reference doc's ranking rule)
    return sorted(items, key=lambda i: (SEV_RANK[i["sev"]], -i["rank"])), sorted(stretch, key=lambda i: -i["rank"])


def ads_candidates(d, excluded, names):
    items, unmapped_spend, unmapped_n = [], 0.0, 0
    for r in d["ads_sku_detail"]:
        if r.get("Category") == "Unmapped Category":
            unmapped_spend += r.get("Ad_Spend_MTD") or 0
            unmapped_n += 1
            continue
        flag = r.get("ROAS_Flag")
        if flag not in SEV_RANK or r["Platform"] in excluded:
            continue
        if (r.get("ROAS_LFM") or 0) <= 0 or (r.get("Ad_Spend_MTD") or 0) < bc.MIN_AD_SPEND_MTD:
            continue
        drop = (r["ROAS_LFM"] - r["ROAS_MTD"]) / r["ROAS_LFM"] * 100
        lost = r["Ad_Spend_MTD"] * (r["ROAS_LFM"] - r["ROAS_MTD"])  # ad sales foregone at LFM efficiency
        items.append({
            "key": f"ads|{r['Platform']}|{r['SKU']}",
            "tab": "ads", "sev": flag, "platform": r["Platform"], "category": r["Category"],
            "sku": r["SKU"], "name": names.get((r["Platform"], r["SKU"]), r["SKU"]),
            "metric": r["ROAS_MTD"], "rank": lost,
            "facts": {
                "roas_mtd": f"{r['ROAS_MTD']:.2f}", "roas_last_month": f"{r['ROAS_LFM']:.2f}",
                "roas_drop": pct(drop),
                "troas_mtd": f"{r.get('TROAS_MTD') or 0:.2f}", "troas_last_month": f"{r.get('TROAS_LFM') or 0:.2f}",
                "ad_spend_mtd": inr(r["Ad_Spend_MTD"]),
                "spend_change_vs_last_month": spct(r.get("Ad_Spend_Delta_vs_LFM")),
                "cpc_change_vs_last_month": spct(r.get("CPC_Delta_vs_LFM")),
                "ad_sales_shortfall_at_last_month_roas": inr(lost),
            },
        })
    items.sort(key=lambda i: (SEV_RANK[i["sev"]], -i["rank"]))
    return items, {"count": unmapped_n, "spend": unmapped_spend}


def discount_candidates(d, excluded):
    items = []
    for r in d["discount_sku"]:
        flag = r.get("Discount_Flag")
        if flag not in SEV_RANK or r["Platform"] in excluded or not r.get("Has_Guideline"):
            continue
        var = r.get("Discount_vs_Guideline")
        if var is None:
            continue
        leak = (r.get("Actual_MRP_Revenue") or 0) * var / 100
        if leak < bc.MIN_DISCOUNT_LEAKAGE:
            continue
        items.append({
            "key": f"discount|{r['Platform']}|{r['SKU']}",
            "tab": "discount", "sev": flag, "platform": r["Platform"], "category": r["Category"],
            "sku": r["SKU"], "name": r.get("Short_Name") or r["SKU"],
            "metric": var, "rank": leak,
            "facts": {
                "actual_discount": pct(r["Actual_Discount_Pct"]),
                "guideline_discount": pct(r["Guideline_Discount_Pct"]),
                "planned_discount": pct(r["Planned_Discount_Pct"]),
                "above_guideline": f"+{var:.1f} pp",
                "discount_given_beyond_guideline_mtd": inr(leak),
                "mtd_revenue": inr(r["Actual_SP_Revenue"]),
            },
        })
    items.sort(key=lambda i: (SEV_RANK[i["sev"]], -i["rank"]))
    return items


# ══════════════════════════════════════════════
#  Memory: patience with managers
# ══════════════════════════════════════════════
def load_state(month):
    try:
        with open(bc.STATE_FILE) as f:
            st = json.load(f)
    except Exception:
        st = {}
    if st.get("month") != month:  # new month → plans and MTD reset, start fresh
        st = {"month": month, "items": {}}
    return st


def got_worse(item, prev) -> bool:
    if SEV_RANK[item["sev"]] < SEV_RANK.get(prev.get("sev"), 1):
        return True
    m0, m1 = prev.get("metric_at_raise"), item["metric"]
    if m0 is None or m1 is None:
        return False
    if item["tab"] == "revenue":
        return m0 - m1 >= bc.ESCALATE_ATTAINMENT_DROP_PTS
    if item["tab"] == "ads":
        return m0 > 0 and (m0 - m1) / m0 * 100 >= bc.ESCALATE_ROAS_DROP_PCT
    return m1 - m0 >= bc.ESCALATE_DISCOUNT_RISE_PP


def classify(item, state):
    prev = state["items"].get(item["key"])
    if not prev:
        return "new"
    item["raised_on"] = prev["first_raised"]
    item["times_raised"] = prev.get("times_raised", 1)
    item["metric_at_raise"] = prev["metric_at_raise"]
    days = (TODAY - dt.date.fromisoformat(prev["last_actioned"])).days
    item["days_since"] = (TODAY - dt.date.fromisoformat(prev["first_raised"])).days
    if days == 0:
        return prev.get("status_today", "new")  # same-day re-run: keep today's status
    if got_worse(item, prev):
        return "escalated"
    if days >= bc.COOLDOWN_DAYS[item["tab"]]:
        return "followup"
    return "waiting"


def trend_text(item):
    m0, m1 = item.get("metric_at_raise"), item["metric"]
    if m0 is None:
        return ""
    if item["tab"] == "revenue":
        return f"attainment {m0:.1f}% → {m1:.1f}%"
    if item["tab"] == "ads":
        return f"ROAS {m0:.2f} → {m1:.2f}"
    return f"above guideline +{m0:.1f} → +{m1:.1f} pp"


def pick(cands, state, limit):
    """Split one tab's candidates into tasks (new/follow-up/escalated) and waiting items."""
    for c in cands:
        c["status"] = classify(c, state)
    order = {"escalated": 0, "followup": 1, "new": 1}
    tasks = sorted([c for c in cands if c["status"] in order], key=lambda c: order[c["status"]])[:limit]
    waiting = [c for c in cands if c["status"] == "waiting"][: bc.MAX_WAITING_SHOWN]
    return tasks, waiting


def improved(state, flagged_keys, platforms):
    out = []
    for k, v in state["items"].items():
        if k not in flagged_keys and k.split("|")[1] in platforms:
            out.append(v)
    return out[:3]


# ══════════════════════════════════════════════
#  Claude: wording only
# ══════════════════════════════════════════════
SYSTEM_PROMPT = """You write the morning action brief for e-commerce growth managers at Nat Habit, an Indian personal-care brand selling across marketplaces.

You receive scorecards for this manager's platforms (KPIs, platforms, ads, top categories, and the categories furthest behind their till-date target with the platforms or SKUs driving each gap), plus a list of action items already selected by rules. Your job is wording only:
1. "focus": exactly 3 bullets, each ONE sentence of at most 25 words, saying where this manager should focus today. Name categories, platforms or SKUs and rupee figures, and connect the dots (e.g. a gap sitting on SKUs where ROAS fell or discount is far above guideline). No generic advice.
2. "actions": for each item id, ONE sentence (max 30 words) naming the single lever to pull today.

Levers by signal:
- revenue, root cause "Units Gap": check stock/availability, buy-box, listing visibility and ad coverage; push demand.
- revenue, root cause "Discount Creep": volume is coming only from over-discounting; pull discount back toward guideline, check unintended coupons/price drops.
- revenue, root cause "Both": slow AND over-discounted; the discount isn't buying volume. Fix pricing and demand together.
- ads, ROAS drop: review targeting, bids, creative; check if spend rose without sales; compare TROAS to see if the organic halo held.
- discount, above guideline: bring the price back toward guideline on that platform; check coupons, deals and platform-funded vs brand-funded discounts.

Tone rules (important):
- Be patient and respectful. Actions take a few days to show up in the numbers, and ad sales are often attributed late.
- "followup" items: the manager was asked about this some days ago and it has not improved yet. Ask them to take a second look, without blame.
- "escalated" items: got clearly worse since first raised; say so plainly and calmly.
- "new" items: just state the lever.
- Never invent numbers or compute new ones. Use only the figures given, exactly as written, and only when helpful.

Reply with ONLY a JSON object: {"focus": [...], "actions": {"<id>": "...", ...}}. No markdown fences."""

FALLBACK_LEVER = {
    ("revenue", "Units Gap"): "Check stock, buy-box, listing visibility and ad coverage, then push demand.",
    ("revenue", "Discount Creep"): "Volume is coming from over-discounting; pull the discount back toward guideline.",
    ("revenue", "Both"): "Slow and over-discounted; fix pricing and demand together.",
    ("revenue", None): "Check availability, visibility and pricing for this SKU.",
    ("ads", None): "Review targeting, bids and creative; check whether spend rose without sales.",
    ("discount", None): "Bring the price back toward guideline; check coupons and deals on this platform.",
}


def fallback_wording(items):
    acts = {}
    for i in items:
        lever = FALLBACK_LEVER.get((i["tab"], i.get("root_cause"))) or FALLBACK_LEVER[(i["tab"], None)]
        if i["status"] == "followup":
            lever = f"Raised {i.get('days_since', 0)} days ago and not improved yet. Worth a second look: {lever[0].lower() + lever[1:]}"
        elif i["status"] == "escalated":
            lever = f"Got worse since first raised. {lever}"
        acts[i["id"]] = lever
    return {"intro": "", "actions": acts}


def fallback_focus(behind):
    out = []
    for r in behind[:3]:
        if r.get("platforms"):
            p = r["platforms"][0]
            out.append(f"{r['name']}: {r['f']['gap']} behind to date; {p['f']['gap']} of it on {p['name']} "
                       f"(ROAS {p['f']['roas']} vs {p['f']['roas_lm']}, discount {p['f']['disc_vs_guideline']} vs guideline).")
    return out


def team_wording(scope_name, sec, items):
    fb = {"focus": fallback_focus(sec["behind"]), "actions": fallback_wording(items)["actions"]}
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log.warning("  No ANTHROPIC_API_KEY set; using rule-based wording")
        return fb
    try:
        import anthropic
        slim = lambda r: {"name": r["name"], **r["f"]}
        payload = {
            "platforms": scope_name, "kpis": sec["kpis"],
            "platform_scorecard": [slim(r) for r in sec["plat_rows"]],
            "top_categories_by_revenue": [slim(r) for r in sec["top_cats"]],
            "categories_furthest_behind": [{**slim(r), "breakdown": [slim(p) for p in r.get("platforms", [])]}
                                           for r in sec["behind"]],
            "items": [{"id": i["id"], "tab": i["tab"], "status": i["status"], "severity": i["sev"],
                       "platform": i["platform"], "category": i["category"], "product": i["name"],
                       "days_since_first_raised": i.get("days_since"), "facts": i["facts"]} for i in items],
        }
        msg = anthropic.Anthropic().messages.create(
            model=os.environ.get("BRIEF_MODEL", bc.MODEL), max_tokens=4000, system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}])
        text = "".join(b.text for b in msg.content if b.type == "text").strip()
        text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        out = json.loads(text)
        focus = [b for b in out.get("focus", []) if isinstance(b, str) and b.strip()][:3] or fb["focus"]
        acts = {i["id"]: out.get("actions", {}).get(i["id"]) or fb["actions"][i["id"]] for i in items}
        usage = getattr(msg, "usage", None)
        log.info(f"  ✓ Claude wording OK for {scope_name[:40]} ({getattr(usage, 'input_tokens', '?')} in / "
                 f"{getattr(usage, 'output_tokens', '?')} out tokens)")
        return {"focus": focus, "actions": acts}
    except Exception as ex:
        log.warning(f"  Claude wording failed for {scope_name[:40]} ({ex}); using rule-based wording")
        return fb


def claude_wording(audience, headline, items):
    if not items:
        return fallback_wording(items)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log.warning("  No ANTHROPIC_API_KEY set; using rule-based wording")
        return fallback_wording(items)
    try:
        import anthropic
        payload = {
            "audience": audience, "headline": headline,
            "items": [{"id": i["id"], "tab": i["tab"], "status": i["status"], "severity": i["sev"],
                       "platform": i["platform"], "category": i["category"], "product": i["name"],
                       "days_since_first_raised": i.get("days_since"), "facts": i["facts"]} for i in items],
        }
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model=os.environ.get("BRIEF_MODEL", bc.MODEL), max_tokens=4000, system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        )
        text = "".join(b.text for b in msg.content if b.type == "text").strip()
        text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        out = json.loads(text)
        fb = fallback_wording(items)["actions"]
        out["actions"] = {i["id"]: out.get("actions", {}).get(i["id"]) or fb[i["id"]] for i in items}
        log.info(f"  ✓ Claude wording OK for {audience[:40]} ({getattr(getattr(msg, 'usage', None), 'input_tokens', '?')} in / {getattr(getattr(msg, 'usage', None), 'output_tokens', '?')} out tokens)")
        return out
    except Exception as e:
        log.warning(f"  Claude wording failed for {audience} ({e}); using rule-based wording")
        return fallback_wording(items)


EXEC_PROMPT = """You write the executive morning brief for the Head of Marketplaces & Quick Commerce at Nat Habit, an Indian personal-care brand. Platform managers already get their own detailed briefs; this one must be short and pointed.

You receive: KPIs, a platform scorecard, an ads scorecard, the top 10 categories by revenue, the 10 categories furthest behind their till-date target with the platforms driving each gap, and items needing the head's attention.

Write:
1. "focus": exactly 3 bullets, each ONE sentence of at most 25 words, saying where the head should focus today. Be specific: name categories, platforms and rupee figures. Connect the dots (e.g. a category's gap sitting mostly on one platform where ROAS fell or discount is far above guideline). No generic advice.
2. "actions": for each attention item id, the lever in at most 12 words.

Rules: use only the numbers given, exactly as written; never compute new ones. D2C is excluded from everything. Actions take a few days to show in the numbers; only treat something as an execution problem if it got worse or was followed up repeatedly.
Reply with ONLY a JSON object: {"focus": [...], "actions": {"<id>": "..."}}. No markdown fences."""


def agg(d, plats, cat=None, factors=None):
    """Revenue, ads and discount numbers for a set of platforms, optionally one category."""
    keep = lambda r: r["Platform"] in plats and (cat is None or r["Category"] == cat)
    cp = [r for r in d["cat_plat_summary"] if keep(r)]
    act, pace = sum(r["Actual_SP_Revenue"] for r in cp), sum(r["Prorated_Planned_SP"] for r in cp)
    ads = [r for r in d["ads_by_cat_plat"] if keep(r)]
    a = {k: sum(r.get(k) or 0 for r in ads) for k in
         ("Ad_Spend_MTD", "Ad_Sales_MTD", "Ad_Units_MTD", "Ad_Clicks_MTD", "Gross_Units_MTD", "Gross_Sales_MTD",
          "Ad_Spend_LFM", "Ad_Sales_LFM", "Ad_Clicks_LFM")}
    tgt = sum((r.get("Ad_Spend_TGT") or 0) * (factors or {}).get(r["Platform"], 0) for r in ads)
    ds = [r for r in d["discount_sku"] if keep(r)]
    mrp, sp = sum(r["Actual_MRP_Revenue"] for r in ds), sum(r["Actual_SP_Revenue"] for r in ds)
    g = [r for r in ds if r.get("Has_Guideline") and r.get("Guideline_Discount_Pct") is not None]
    gw = sum(r["Actual_MRP_Revenue"] for r in g)
    disc = (mrp - sp) / mrp * 100 if mrp else None
    guide = sum(r["Guideline_Discount_Pct"] * r["Actual_MRP_Revenue"] for r in g) / gw if gw else None
    div = lambda x, y: x / y if y else None
    cpc_m, cpc_l = div(a["Ad_Spend_MTD"], a["Ad_Clicks_MTD"]), div(a["Ad_Spend_LFM"], a["Ad_Clicks_LFM"])
    return {
        "revenue": act, "pace": pace, "gap": act - pace if pace else None,
        "attainment": div(act, pace) * 100 if pace else None,
        "spend": a["Ad_Spend_MTD"], "ad_sales": a["Ad_Sales_MTD"],
        "roas": div(a["Ad_Sales_MTD"], a["Ad_Spend_MTD"]), "roas_lm": div(a["Ad_Sales_LFM"], a["Ad_Spend_LFM"]),
        "troas": div(a["Gross_Sales_MTD"], a["Ad_Spend_MTD"]),
        "cpc_chg": (cpc_m / cpc_l - 1) * 100 if cpc_m and cpc_l else None,
        "ad_contrib": div(a["Ad_Units_MTD"], a["Gross_Units_MTD"]) * 100 if a["Gross_Units_MTD"] else None,
        "spend_vs_tgt": div(a["Ad_Spend_MTD"], tgt) * 100 if tgt else None,
        "disc": disc, "guide": guide,
        "disc_vs_guideline": disc - guide if disc is not None and guide is not None else None,
    }


def fmt_row(r):
    """String versions of every metric, for display and for Claude."""
    f2 = lambda x: f"{x:.2f}" if x else "–"
    return {
        "revenue": inr(r["revenue"]), "attainment": pct(r["attainment"]),
        "gap": inr(r["gap"]) if r["gap"] is not None else "no plan",
        "spend": inr(r["spend"]) if r["spend"] else "no ads", "ad_sales": inr(r["ad_sales"]) if r["spend"] else "–",
        "roas": f2(r["roas"]), "roas_lm": f2(r["roas_lm"]), "troas": f2(r["troas"]),
        "cpc_chg": spct(r["cpc_chg"]), "ad_contrib": pct(r["ad_contrib"], 0),
        "spend_vs_tgt": pct(r["spend_vs_tgt"], 0),
        "disc_vs_guideline": "–" if r["disc_vs_guideline"] is None else f"{round(r['disc_vs_guideline'], 1) + 0.0:+.1f} pp".replace("-", "−").replace("+0.0", "0.0"),
    }


# ── colour helpers (thresholds from the dashboard summary) ──
def _att_style(a, s):
    if a is None:
        return ""
    if a < s.get("attainment_critical_pct", 70):
        return ";background:#ffebe9;color:#cf222e;font-weight:bold"
    if a < s.get("attainment_warning_pct", 85):
        return ";background:#fff8c5;color:#9a6700;font-weight:bold"
    return ";color:#1a7f37;font-weight:bold"


def _roas_style(r, s):
    if not r["roas"] or not r["roas_lm"]:
        return ""
    drop = (r["roas_lm"] - r["roas"]) / r["roas_lm"]
    if drop > s.get("roas_drop_critical", 0.2):
        return ";color:#cf222e;font-weight:bold"
    return ";color:#9a6700" if drop > s.get("roas_drop_warning", 0.05) else ""


def _disc_style(v, s):
    if v is None:
        return ""
    if v > s.get("discount_critical", 0.05) * 100:
        return ";color:#cf222e;font-weight:bold"
    return ";color:#9a6700" if v > s.get("discount_warning", 0.03) * 100 else ""


TH = "text-align:left;padding:5px 8px;border-bottom:2px solid #d0d7de;font-size:11px;color:#59636e;text-transform:uppercase;white-space:nowrap"
TD = "padding:5px 8px;border-bottom:1px solid #eaeef2;font-size:13px;white-space:nowrap"


def table(headers, rows_html):
    head = "".join(f'<th style="{TH}">{h}</th>' for h in headers)
    return (f'<div style="overflow-x:auto"><table style="border-collapse:collapse;width:100%">'
            f'<tr>{head}</tr>{rows_html}</table></div>')


def render_platform_scorecard(rows, info_rows, s, show_owner=True):
    out = ""
    for r in rows + info_rows:
        f, grey = r["f"], r.get("info")
        st = ";color:#8c959f" if grey else ""
        name = e(r["name"]) + (' <span style="font-weight:normal">(info only)</span>' if grey else "")
        out += (f'<tr><td style="{TD}{st}"><b title="{e(r.get("title", ""))}">{name}</b></td>'
                + (f'<td style="{TD};color:#59636e">{e(r.get("owner", ""))}</td>' if show_owner else "")
                + f'<td style="{TD}{st}">{f["revenue"]}</td>'
                f'<td style="{TD}{"" if grey else _att_style(r["attainment"], s)}{st}">{f["attainment"]}</td>'
                f'<td style="{TD}{st}">{f["gap"]}</td>'
                f'<td style="{TD}{"" if grey else _roas_style(r, s)}{st}">{f["roas"]} <span style="color:#59636e;font-weight:normal">({f["roas_lm"]})</span></td>'
                f'<td style="{TD}{"" if grey else _disc_style(r["disc_vs_guideline"], s)}{st}">{f["disc_vs_guideline"]}</td>'
                f'<td style="{TD};text-align:center{st}">{r.get("tasks") or ""}</td></tr>')
    cols = ["Platform", "Owner", "Revenue MTD", "vs pace", "Gap", "ROAS (LM)", "Disc vs guide", "Tasks"]
    return table(cols if show_owner else [c for c in cols if c != "Owner"], out)


def render_ads_scorecard(rows, s):
    out = ""
    for r in rows:
        f = r["f"]
        if not r["spend"]:
            out += (f'<tr><td style="{TD}"><b title="{e(r.get("title", ""))}">{e(r["name"])}</b></td>'
                    f'<td style="{TD};color:#8c959f" colspan="7">no ad spend this month</td></tr>')
            continue
        out += (f'<tr><td style="{TD}"><b title="{e(r.get("title", ""))}">{e(r["name"])}</b></td>'
                f'<td style="{TD}">{f["spend"]}</td><td style="{TD}">{f["ad_sales"]}</td>'
                f'<td style="{TD}{_roas_style(r, s)}">{f["roas"]} <span style="color:#59636e;font-weight:normal">({f["roas_lm"]})</span></td>'
                f'<td style="{TD}">{f["troas"]}</td><td style="{TD}">{f["cpc_chg"]}</td>'
                f'<td style="{TD}">{f["ad_contrib"]}</td><td style="{TD}">{f["spend_vs_tgt"]}</td></tr>')
    return table(["Platform", "Spend MTD", "Ad sales", "ROAS (LM)", "TROAS", "CPC vs LM", "Ad contrib.", "Spend vs target"], out)


def render_category_scorecard(rows, s):
    out = ""
    for n, r in enumerate(rows, 1):
        f = r["f"]
        out += (f'<tr><td style="{TD};color:#59636e">{n}</td><td style="{TD}"><b>{e(r["name"])}</b></td>'
                f'<td style="{TD}">{f["revenue"]}</td><td style="{TD}{_att_style(r["attainment"], s)}">{f["attainment"]}</td>'
                f'<td style="{TD}">{f["gap"]}</td><td style="{TD}">{f["spend"]}</td>'
                f'<td style="{TD}{_roas_style(r, s)}">{f["roas"]} <span style="color:#59636e;font-weight:normal">({f["roas_lm"]})</span></td>'
                f'<td style="{TD}{_disc_style(r["disc_vs_guideline"], s)}">{f["disc_vs_guideline"]}</td></tr>')
    return table(["#", "Category", "Revenue MTD", "vs pace", "Gap", "Ad spend", "ROAS (LM)", "Disc vs guide"], out)


def render_focus(rows, s, what="platforms"):
    out = ""
    for n, r in enumerate(rows, 1):
        f = r["f"]
        out += (f'<tr style="background:#f6f8fa"><td style="{TD};color:#59636e">{n}</td>'
                f'<td style="{TD}"><b>{e(r["name"])}</b></td>'
                f'<td style="{TD}"><b>{f["gap"]}</b></td><td style="{TD}{_att_style(r["attainment"], s)}">{f["attainment"]}</td>'
                f'<td style="{TD}">{f["spend"]}</td>'
                f'<td style="{TD}{_roas_style(r, s)}">{f["roas"]} <span style="color:#59636e;font-weight:normal">({f["roas_lm"]})</span></td>'
                f'<td style="{TD}{_disc_style(r["disc_vs_guideline"], s)}">{f["disc_vs_guideline"]}</td></tr>')
        for p in r["platforms"]:
            g = p["f"]
            out += (f'<tr><td style="{TD}"></td><td style="{TD};padding-left:22px">↳ {e(p["name"])} '
                    f'<span style="color:#59636e">· {e(p.get("owner", ""))}</span></td>'
                    f'<td style="{TD}">{g["gap"]}</td><td style="{TD}{_att_style(p["attainment"], s)}">{g["attainment"]}</td>'
                    f'<td style="{TD}">{g["spend"]}</td>'
                    f'<td style="{TD}{_roas_style(p, s)}">{g["roas"]} <span style="color:#59636e;font-weight:normal">({g["roas_lm"]})</span></td>'
                    f'<td style="{TD}{_disc_style(p["disc_vs_guideline"], s)}">{g["disc_vs_guideline"]}</td></tr>')
    return table(["#", f"Category → {what}", "Gap to date", "vs pace", "Ad spend", "ROAS (LM)", "Disc vs guide"], out)


def exec_wording(kpis, plat_rows, ads_rows, cat_rows, focus_rows, attention):
    fb = fallback_wording(attention)["actions"]
    fb = {k: v.split(";")[0].split(". Worth")[0] for k, v in fb.items()}
    focus = []
    for r in focus_rows[:3]:
        if r["platforms"]:
            p = r["platforms"][0]
            focus.append(f"{r['name']}: {r['f']['gap']} behind to date; {p['f']['gap']} of it on {p['name']} "
                         f"(ROAS {p['f']['roas']} vs {p['f']['roas_lm']}, discount {p['f']['disc_vs_guideline']} vs guideline).")
    fallback = {"focus": focus, "actions": fb}
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log.warning("  No ANTHROPIC_API_KEY set; using rule-based wording (executive)")
        return fallback
    try:
        import anthropic
        slim = lambda r: {"name": r["name"], **r["f"]}
        payload = {
            "kpis": kpis,
            "platform_scorecard": [slim(r) for r in plat_rows],
            "ads_scorecard": [slim(r) for r in ads_rows],
            "top_categories_by_revenue": [slim(r) for r in cat_rows],
            "categories_furthest_behind": [{**slim(r), "platforms": [slim(p) for p in r["platforms"]]} for r in focus_rows],
            "needs_attention": [{"id": i["id"], "tab": i["tab"], "status": i["status"], "times_raised": i.get("times_raised"),
                                 "platform": i["platform"], "product": i["name"], "facts": i["facts"]} for i in attention],
        }
        msg = anthropic.Anthropic().messages.create(
            model=os.environ.get("BRIEF_MODEL", bc.MODEL), max_tokens=1500, system=EXEC_PROMPT,
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}])
        text = "".join(b.text for b in msg.content if b.type == "text").strip()
        text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        out = json.loads(text)
        fl = [b for b in out.get("focus", []) if isinstance(b, str) and b.strip()][:3]
        acts = {k: out.get("actions", {}).get(k) or v for k, v in fb.items()}
        usage = getattr(msg, "usage", None)
        log.info(f"  ✓ Claude wording OK for executive brief ({getattr(usage, 'input_tokens', '?')} in / "
                 f"{getattr(usage, 'output_tokens', '?')} out tokens)")
        return {"focus": fl or focus, "actions": acts}
    except Exception as ex:
        log.warning(f"  Claude wording failed for executive brief ({ex}); using rule-based wording")
        return fallback


SHORT_FACT = {
    "revenue": lambda f: f"{f['attainment_vs_pace']} of pace, gap {f['revenue_gap']}, {f['root_cause']}",
    "ads": lambda f: f"ROAS {f['roas_mtd']} vs {f['roas_last_month']}, spend {f['ad_spend_mtd']}",
    "discount": lambda f: f"{f['actual_discount']} vs {f['guideline_discount']} guideline, {f['discount_given_beyond_guideline_mtd']} extra",
}


def render_compact(items, actions, show_owner=None):
    li = ""
    for i in items:
        tag = ""
        if i.get("status") == "escalated":
            tag = " " + badge("escalated", "Got worse")
        elif i.get("status") == "followup":
            tag = " " + badge("followup", f"Raised {i.get('times_raised', 1)}×")
        owner = f' <span style="{CSS["muted"]}">· {e(show_owner.get(i["platform"], ""))}</span>' if show_owner else ""
        li += (f'<li style="margin:3px 0;font-size:13px">{badge(i["sev"], i["sev"][0].upper())}{tag} '
               f'<b>{e(i["platform"])}</b> — {e(i["name"])}: {SHORT_FACT[i["tab"]](i["facts"])}{owner}'
               f'<br><span style="color:#0b5394">→ {e(actions.get(i["id"], ""))}</span></li>')
    return f'<ul style="margin:0;padding-left:20px;list-style:none">{li}</ul>'


# ══════════════════════════════════════════════
#  Headlines
# ══════════════════════════════════════════════
def scope_headline(d, platforms):
    cp = [r for r in d["cat_plat_summary"] if r["Platform"] in platforms]
    act = sum(r["Actual_SP_Revenue"] for r in cp)
    pace = sum(r["Prorated_Planned_SP"] for r in cp)
    ads = [r for r in d["ads_by_plat"] if r["Platform"] in platforms]
    sp_m, sa_m = sum(r["Ad_Spend_MTD"] for r in ads), sum(r["Ad_Sales_MTD"] for r in ads)
    sp_l, sa_l = sum(r["Ad_Spend_LFM"] for r in ads), sum(r["Ad_Sales_LFM"] for r in ads)
    ds = [r for r in d["discount_sku"] if r["Platform"] in platforms]
    mrp, sp = sum(r["Actual_MRP_Revenue"] for r in ds), sum(r["Actual_SP_Revenue"] for r in ds)
    g = [r for r in ds if r.get("Has_Guideline") and r.get("Guideline_Discount_Pct") is not None]
    gw = sum(r["Actual_MRP_Revenue"] for r in g)
    return {
        "attainment_vs_pace": pct(act / pace * 100) if pace else "–",
        "revenue_mtd": inr(act), "revenue_gap": inr(act - pace),
        "roas_mtd": f"{sa_m / sp_m:.2f}" if sp_m else "–",
        "roas_last_month": f"{sa_l / sp_l:.2f}" if sp_l else "–",
        "actual_discount": pct((mrp - sp) / mrp * 100) if mrp else "–",
        "guideline_discount": pct(sum(r["Guideline_Discount_Pct"] * r["Actual_MRP_Revenue"] for r in g) / gw) if gw else "–",
    }


# ══════════════════════════════════════════════
#  Email rendering
# ══════════════════════════════════════════════
CSS = {
    "body": "font-family:Arial,Helvetica,sans-serif;color:#1f2328;max-width:760px;margin:0 auto;font-size:14px;line-height:1.45",
    "h1": "font-size:20px;margin:0 0 4px", "sub": "color:#59636e;font-size:12px;margin:0 0 16px",
    "h2": "font-size:16px;margin:24px 0 8px;padding-bottom:4px;border-bottom:2px solid #d0d7de",
    "h3": "font-size:15px;margin:28px 0 6px;color:#0b5394",
    "kpi": "display:inline-block;margin:0 16px 8px 0", "kl": "color:#59636e;font-size:11px;text-transform:uppercase",
    "card": "border:1px solid #d0d7de;border-radius:6px;padding:10px 12px;margin:8px 0",
    "muted": "color:#59636e;font-size:12px",
}
BADGE = {
    "critical": "background:#ffebe9;color:#cf222e", "warning": "background:#fff8c5;color:#9a6700",
    "new": "background:#ddf4ff;color:#0969da", "followup": "background:#fbefff;color:#8250df",
    "escalated": "background:#cf222e;color:#fff",
}
STATUS_LABEL = {"new": "New", "followup": "Follow-up", "escalated": "Got worse"}


def badge(kind, text):
    return f'<span style="{BADGE[kind]};padding:1px 6px;border-radius:10px;font-size:11px;font-weight:bold">{text}</span>'


def e(s):
    return html.escape(str(s))


FACT_LINES = {
    "revenue": lambda f: f"At <b>{f['attainment_vs_pace']}</b> of pace · gap <b>{f['revenue_gap']}</b> · "
                         f"units {f['units_attainment']} · root cause <b>{e(f['root_cause'])}</b> · "
                         f"discount {f['actual_discount']} vs guideline {f['guideline_discount']} · vs last month {f['vs_last_month']}",
    "ads": lambda f: f"ROAS <b>{f['roas_mtd']}</b> vs {f['roas_last_month']} last month (−{f['roas_drop']}) · "
                     f"TROAS {f['troas_mtd']} vs {f['troas_last_month']} · spend {f['ad_spend_mtd']} "
                     f"({f['spend_change_vs_last_month']} vs LM) · CPC {f['cpc_change_vs_last_month']} vs LM",
    "discount": lambda f: f"Discount <b>{f['actual_discount']}</b> vs guideline {f['guideline_discount']} "
                          f"(<b>{f['above_guideline']}</b>) · plan {f['planned_discount']} · "
                          f"<b>{f['discount_given_beyond_guideline_mtd']}</b> given beyond guideline MTD",
}


def render_task(i, action):
    since = ""
    if i["status"] != "new":
        since = f' <span style="{CSS["muted"]}">first raised {e(i.get("raised_on"))} · {e(trend_text(i))}</span>'
    return (f'<div style="{CSS["card"]}">'
            f'{badge(i["sev"], i["sev"].title())} {badge(i["status"], STATUS_LABEL[i["status"]])} '
            f'<b>{e(i["category"])} / {e(i["platform"])}</b> — {e(i["name"])} '
            f'<span style="{CSS["muted"]}">({e(i["sku"])})</span>{since}<br>'
            f'<span style="font-size:13px">{FACT_LINES[i["tab"]](i["facts"])}</span><br>'
            f'<span style="font-size:13px">→ {e(action)}</span></div>')


def render_tab(tab, tasks, waiting, actions):
    out = [f'<h2 style="{CSS["h2"]}">{TAB_TITLES[tab]}</h2>']
    if tasks:
        out += [render_task(i, actions.get(i["id"], "")) for i in tasks]
    else:
        out.append(f'<p style="{CSS["muted"]}">Nothing new to act on here today.</p>')
    if waiting:
        rows = "".join(f"<li>{e(i['category'])} / {e(i['platform'])} — {e(i['name'])}: {e(trend_text(i))} "
                       f"(raised {e(i.get('raised_on'))})</li>" for i in waiting)
        out.append(f'<p style="margin:10px 0 2px"><b>⏳ Give it time</b> '
                   f'<span style="{CSS["muted"]}">— already raised; changes can take a few days to show</span></p>'
                   f'<ul style="margin:0;font-size:13px">{rows}</ul>')
    return "".join(out)


def render_headline(h):
    kp = [("Attainment vs pace", h["attainment_vs_pace"]), ("Revenue MTD", h["revenue_mtd"]),
          ("Gap to pace", h["revenue_gap"]), ("ROAS (MTD vs last month)", f'{h["roas_mtd"]} vs {h["roas_last_month"]}'),
          ("Discount vs guideline", f'{h["actual_discount"]} vs {h["guideline_discount"]}')]
    return "".join(f'<div style="{CSS["kpi"]}"><div style="{CSS["kl"]}">{e(k)}</div><b>{e(v)}</b></div>' for k, v in kp)


def render_email(title, sub, intro, headline_html, body_html, notes):
    note_html = ""
    if notes:
        note_html = (f'<h2 style="{CSS["h2"]}">📝 Notes</h2><ul style="font-size:13px">'
                     + "".join(f"<li>{n}</li>" for n in notes) + "</ul>")
    return (f'<div style="{CSS["body"]}"><h1 style="{CSS["h1"]}">{e(title)}</h1>'
            f'<p style="{CSS["sub"]}">{e(sub)}</p>{headline_html}'
            + (f'<p style="margin:12px 0">{e(intro)}</p>' if intro else "")
            + body_html + note_html
            + f'<p style="{CSS["muted"]};margin-top:24px">Basis: time-prorated target, same as the dashboard flags. '
              f'Thresholds come from the dashboard config. Items already raised wait '
              f'{bc.COOLDOWN_DAYS["revenue"]} days before being raised again, unless they get clearly worse.</p></div>')


# ══════════════════════════════════════════════
#  Sending
# ══════════════════════════════════════════════
def send(to, subject, body):
    if MODE == "dry":
        return
    if MODE == "test":
        subject = f"[TEST → {', '.join(to)}] {subject}"
        to = [os.environ["TEST_RECIPIENT"]]
    user, pw = os.environ["GMAIL_USER"], os.environ["GMAIL_APP_PASSWORD"]
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, f"Category Intelligence <{user}>", ", ".join(to)
    msg.attach(MIMEText(body, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pw)
        s.sendmail(user, to, msg.as_string())
    log.info(f"  Sent → {', '.join(to)}")


# ══════════════════════════════════════════════
#  Main
# ══════════════════════════════════════════════
def build_sections(d, scope, owner_of, tasks_by_plat, factors, group_small, sku_breakdown):
    """Everything the executive-style format needs, for one set of platforms."""
    def row(name, plats, cat=None, **extra):
        r = {"name": name, **agg(d, set(plats), cat, factors), **extra}
        r["f"] = fmt_row(r)
        return r

    tot = row("Total", scope)
    kpis = {"attainment_vs_pace": tot["f"]["attainment"], "revenue_mtd": tot["f"]["revenue"],
            "revenue_gap": tot["f"]["gap"], "roas_mtd": tot["f"]["roas"], "roas_last_month": tot["f"]["roas_lm"],
            "actual_discount": pct(tot["disc"]), "guideline_discount": pct(tot["guide"])}

    prow = [row(p, [p], owner=owner_of.get(p, ""), tasks=tasks_by_plat.get(p, 0)) for p in scope]
    total_rev = sum(r["revenue"] for r in prow) or 1
    cut = bc.SCORECARD_MIN_SHARE if group_small else 0
    plat_rows = sorted([r for r in prow if r["revenue"] / total_rev >= cut], key=lambda r: -r["revenue"])
    small = sorted(r["name"] for r in prow if r["revenue"] / total_rev < cut)
    if small:
        plat_rows.append(row(f"Others ({len(small)})", small, title=", ".join(small),
                             tasks=sum(tasks_by_plat.get(p, 0) for p in small)))

    cats = sorted({r["Category"] for r in d["cat_plat_summary"] if r["Platform"] in scope})
    crow = [row(c, scope, c) for c in cats]
    top_cats = sorted(crow, key=lambda r: -r["revenue"])[: bc.TOP_CATEGORIES]
    behind = sorted([r for r in crow if r["gap"] is not None and r["gap"] < 0], key=lambda r: r["gap"])[: bc.TOP_CATEGORIES]

    if sku_breakdown:  # single platform: show the SKUs driving each category's gap
        p = scope[0]
        ads_sku = {r["SKU"]: r for r in d["ads_sku_detail"] if r["Platform"] == p}
        for r in behind:
            subs = []
            for k in d["sku_detail"]:
                if k["Platform"] != p or k["Category"] != r["name"]:
                    continue
                pace = k.get("Prorated Planned SP Revenue") or 0
                if not pace or k["Revenue Gap"] >= 0:
                    continue
                a = ads_sku.get(k["SKU"], {})
                sp = a.get("Ad_Spend_MTD") or 0
                x = {"name": k.get("Short Name") or k["SKU"], "owner": k["SKU"], "revenue": k["MTD Actual SP Revenue"],
                     "pace": pace, "gap": k["Revenue Gap"], "attainment": k["Revenue Attainment %"],
                     "spend": sp, "ad_sales": a.get("Ad_Sales_MTD") or 0,
                     "roas": a.get("ROAS_MTD") or None if sp else None, "roas_lm": a.get("ROAS_LFM") or None,
                     "troas": None, "cpc_chg": None, "ad_contrib": None, "spend_vs_tgt": None,
                     "disc": k.get("Actual Discount %"), "guide": k.get("Guideline Discount %"),
                     "disc_vs_guideline": k.get("Discount vs Guideline Variance")}
                x["f"] = fmt_row(x)
                subs.append(x)
            r["platforms"] = sorted(subs, key=lambda x: x["gap"])[: bc.FOCUS_PLATFORMS_PER_CATEGORY]
    else:
        for r in behind:
            ps = [row(p, [p], r["name"], owner=owner_of.get(p, "")) for p in scope]
            r["platforms"] = sorted([p for p in ps if p["gap"] is not None and p["gap"] < 0],
                                    key=lambda p: p["gap"])[: bc.FOCUS_PLATFORMS_PER_CATEGORY]
    return {"kpis": kpis, "plat_rows": plat_rows, "ads_rows": plat_rows, "top_cats": top_cats,
            "behind": behind, "sku_breakdown": sku_breakdown}


def render_sections(sec, s, focus, excluded_here=(), info_rows=(), show_owner=True):
    body = ""
    if focus:
        body += (f'<h2 style="{CSS["h2"]}">🎯 Where to focus</h2><ul style="margin:0;padding-left:20px">'
                 + "".join(f'<li style="margin:4px 0">{e(b)}</li>' for b in focus) + "</ul>")
    body += f'<h2 style="{CSS["h2"]}">Platform scorecard</h2>' + render_platform_scorecard(
        sec["plat_rows"], list(info_rows), s, show_owner)
    if excluded_here:
        body += f'<p style="{CSS["muted"]}">Awaiting data this month: {e(", ".join(sorted(excluded_here)))} (not counted as a miss).</p>'
    body += f'<h2 style="{CSS["h2"]}">Ads scorecard</h2>' + render_ads_scorecard(sec["ads_rows"], s)
    body += (f'<p style="{CSS["muted"]}">ROAS in brackets = last full month. Spend vs target = MTD spend against the '
             f'ad plan prorated to date.</p>')
    body += (f'<h2 style="{CSS["h2"]}">Top {len(sec["top_cats"])} categories by revenue</h2>'
             + render_category_scorecard(sec["top_cats"], s))
    what = "SKUs" if sec["sku_breakdown"] else "platforms"
    body += (f'<h2 style="{CSS["h2"]}">Where the gap is: {len(sec["behind"])} categories furthest behind target to date</h2>'
             f'<p style="{CSS["muted"]};margin:0 0 6px">Each with the {bc.FOCUS_PLATFORMS_PER_CATEGORY} {what} '
             f'driving most of its gap.</p>' + render_focus(sec["behind"], s, what))
    return body


def main():
    global MODE
    log.info(f"brief.py – mode={MODE}")
    month, label, d = load_latest()
    s = d["summary"]
    excluded = set(s.get("excluded_platforms") or [])
    all_plats = list(d["prorate_info"].keys())
    teams = team_scopes(all_plats)
    as_of = as_of_ist(d["meta"].get("fetched_at", ""))
    names = {(r["Platform"], r["SKU"]): r.get("Short Name") or r["SKU"] for r in d["sku_detail"]}
    factors = {p: v.get("factor", 0) for p, v in d["prorate_info"].items()}

    rev, stretch = revenue_candidates(d, excluded)
    ads, unmapped = ads_candidates(d, excluded, names)
    disc = discount_candidates(d, excluded)
    cands = {"revenue": rev, "ads": ads, "discount": disc}
    flagged_keys = {c["key"] for lst in cands.values() for c in lst}

    state = load_state(month)
    cutoff = os.environ.get("BRIEF_NOT_AFTER", "").strip()
    if MODE == "send" and cutoff and dt.datetime.now(IST).strftime("%H:%M") > cutoff:
        log.warning(f"  Started after the {cutoff} cut-off for the backup run; no emails sent.")
        MODE = "dry"
    if MODE == "send" and state.get("last_sent") == TODAY.isoformat() and os.environ.get("BRIEF_FORCE") != "yes":
        log.warning("  Real briefs were already sent today; switching to dry run (no emails). "
                    "To send again on purpose, run the workflow manually with brief_mode = send.")
        MODE = "dry"
    os.makedirs(bc.OUTPUT_DIR, exist_ok=True)
    sub = f"Data as of {as_of} · {label} · {TODAY.strftime('%a %d %b %Y')}"
    shown = {}  # key -> item shown as a task today (for memory)

    owner_of = {}
    for t in teams:
        who = ", ".join(x.split("@")[0].split(".")[0].title() for x in t["emails"])
        for p in t["platforms"]:
            owner_of.setdefault(p, who)

    # ── One email per person, covering all their platforms; people with identical platforms share one email ──
    person_plats, person_teams = {}, {}
    for t in teams:
        for em in t["emails"]:
            person_plats.setdefault(em.lower(), set()).update(t["platforms"])
            person_teams.setdefault(em.lower(), []).append(t["name"])
    groups = {}
    for em, plats in person_plats.items():
        groups.setdefault(frozenset(plats), []).append(em)

    # Tasks are picked per team (up to MAX_TASKS_PER_TAB each), so a person in two teams gets both sets
    team_tasks = {}
    for ti, t in enumerate(teams):
        team_tasks[t["name"]] = {}
        for tab, lst in cands.items():
            mine = [dict(c) for c in lst if c["platform"] in t["platforms"]]
            tasks, waiting = pick(mine, state, bc.MAX_TASKS_PER_TAB)
            for n, c in enumerate(tasks):
                c["id"] = f"t{ti}{tab[0]}{n + 1}"
                shown[c["key"]] = c
            team_tasks[t["name"]][tab] = (tasks, waiting)

    for plats, emails in groups.items():
        live = sorted(p for p in plats if p not in excluded)
        if not live:
            continue
        my_teams = []
        for em in emails:
            for n in person_teams[em]:
                if n not in my_teams:
                    my_teams.append(n)
        tab_out, ids, mine_counts = {}, [], {}
        for tab in cands:
            tasks = [c for n in my_teams for c in team_tasks[n][tab][0]]
            waiting = [c for n in my_teams for c in team_tasks[n][tab][1]][: bc.MAX_WAITING_SHOWN]
            order = {"escalated": 0}
            tasks.sort(key=lambda c: (order.get(c["status"], 1), SEV_RANK[c["sev"]], -c["rank"]))
            for c in tasks:
                mine_counts[c["platform"]] = mine_counts.get(c["platform"], 0) + 1
            tab_out[tab] = (tasks, waiting)
            ids += tasks
        sec = build_sections(d, live, owner_of, mine_counts, factors, group_small=False,
                             sku_breakdown=len(live) == 1)
        words = team_wording(", ".join(live), sec, ids)
        body = render_sections(sec, s, words["focus"], excluded & plats, show_owner=False)
        body += f'<h2 style="{CSS["h2"]};border-bottom-color:#0b5394">✅ Today’s tasks</h2>'
        body += "".join(render_tab(tab, *tab_out[tab], words["actions"]) for tab in cands)
        notes = []
        st = [i for i in stretch if i["platform"] in plats][:3]
        if st:
            notes.append("Plan may be the issue, not execution (target set >1.3× recent run-rate): "
                         + "; ".join(f"{e(i['name'])} on {e(i['platform'])} at {i['facts']['attainment_vs_pace']}" for i in st))
        good = improved(state, flagged_keys, plats)
        if good:
            notes.append("✅ No longer flagged since raised: " + "; ".join(
                f"{e(v['name'])} on {e(v['platform'])} ({e(v['tab'])})" for v in good))
        title = "Morning brief · " + " + ".join(my_teams)
        out = render_email(title, sub, "", render_headline(sec["kpis"]), body, notes)
        for em in emails:
            open(f"{bc.OUTPUT_DIR}/{em.split('@')[0]}.html", "w", encoding="utf-8").write(out)
        send(emails, f"{title} · {TODAY.strftime('%d %b')}", out)

    # ── Master brief: executive view (marketplace + quick commerce only) ──
    info_only = set(getattr(bc, "INFO_ONLY", []))
    scope = [p for p in all_plats if p not in excluded and p not in info_only]
    tasks_by_plat = {}
    for c in shown.values():
        tasks_by_plat[c["platform"]] = tasks_by_plat.get(c["platform"], 0) + 1
    sec = build_sections(d, scope, owner_of, tasks_by_plat, factors, group_small=True, sku_breakdown=False)
    info_rows = []
    for p in all_plats:
        if p in info_only:
            r = {"name": p, **agg(d, {p}, None, factors), "owner": "", "info": True}
            r["f"] = fmt_row(r)
            info_rows.append(r)

    attention = []
    for tab, lst in cands.items():
        for c in lst:
            if c["platform"] not in scope:
                continue
            c2 = dict(c)
            c2["status"] = classify(c2, state)
            if c2["status"] == "escalated" or (c2["status"] == "followup" and c2.get("times_raised", 1) >= 2):
                attention.append(c2)
    attention.sort(key=lambda i: (SEV_RANK[i["sev"]], -i["rank"]))
    attention = attention[: bc.MASTER_MAX_ATTENTION]
    for n, c in enumerate(attention):
        c["id"] = f"x{n + 1}"

    w = exec_wording(sec["kpis"], sec["plat_rows"], sec["ads_rows"], sec["top_cats"], sec["behind"], attention)
    body = render_sections(sec, s, w["focus"], excluded, info_rows)
    if attention:
        body += (f'<h2 style="{CSS["h2"]}">🚩 Needs your attention</h2>'
                 f'<p style="{CSS["muted"]};margin:0 0 4px">Got worse since raised, or not improving after repeated follow-ups.</p>'
                 + render_compact(attention, w["actions"], show_owner=owner_of))
    title = "Morning brief · Executive view"
    sub_x = sub + " · Marketplaces & quick commerce (D2C shown for information only)"
    out = render_email(title, sub_x, "", render_headline(sec["kpis"]), body, [])
    open(f"{bc.OUTPUT_DIR}/master.html", "w", encoding="utf-8").write(out)
    send(bc.MASTER["emails"], f"{title} · {TODAY.strftime('%d %b')}", out)

    # ── Remember what was raised (real sends only) ──
    if MODE == "send":
        items = state["items"]
        for k in list(items):
            if k not in flagged_keys:
                del items[k]  # resolved; reported once above
        for k, c in shown.items():
            prev = items.get(k)
            first = prev["first_raised"] if prev else TODAY.isoformat()
            items[k] = {"tab": c["tab"], "platform": c["platform"], "name": c["name"], "sev": c["sev"],
                        "first_raised": first, "last_actioned": TODAY.isoformat(), "status_today": c["status"],
                        "times_raised": (prev.get("times_raised", 1) + (prev["last_actioned"] != TODAY.isoformat())) if prev else 1,
                        "metric_at_raise": prev["metric_at_raise"] if prev else c["metric"], "metric_last": c["metric"]}
        state["last_sent"] = TODAY.isoformat()
        os.makedirs(os.path.dirname(bc.STATE_FILE), exist_ok=True)
        with open(bc.STATE_FILE, "w") as f:
            json.dump(state, f, indent=1, ensure_ascii=False)
        log.info(f"  Memory saved: {len(items)} open items")
    log.info("✅ Done")


if __name__ == "__main__":
    main()
