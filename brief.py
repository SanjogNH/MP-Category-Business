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
          "ads_by_plat", "ads_sku_detail", "discount_by_cat", "discount_sku"]


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

You receive a JSON list of action items already selected by rules. Your job is wording only:
1. "intro": 2-3 sentences for the top of the email, summarising the situation for this audience.
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

Reply with ONLY a JSON object: {"intro": "...", "actions": {"<id>": "...", ...}}. No markdown fences."""

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
        log.info(f"  ✓ Claude wording OK for {audience[:40]} ({msg.usage.input_tokens} in / {msg.usage.output_tokens} out tokens)")
        return out
    except Exception as e:
        log.warning(f"  Claude wording failed for {audience} ({e}); using rule-based wording")
        return fallback_wording(items)


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
def main():
    log.info(f"brief.py – mode={MODE}")
    month, label, d = load_latest()
    s = d["summary"]
    excluded = set(s.get("excluded_platforms") or [])
    all_plats = list(d["prorate_info"].keys())
    teams = team_scopes(all_plats)
    as_of = as_of_ist(d["meta"].get("fetched_at", ""))
    names = {(r["Platform"], r["SKU"]): r.get("Short Name") or r["SKU"] for r in d["sku_detail"]}

    rev, stretch = revenue_candidates(d, excluded)
    ads, unmapped = ads_candidates(d, excluded, names)
    disc = discount_candidates(d, excluded)
    cands = {"revenue": rev, "ads": ads, "discount": disc}
    flagged_keys = {c["key"] for lst in cands.values() for c in lst}

    state = load_state(month)
    os.makedirs(bc.OUTPUT_DIR, exist_ok=True)
    sub = f"Data as of {as_of} · {label} · {TODAY.strftime('%a %d %b %Y')}"
    shown = {}  # key -> item shown as a task today (for memory)
    team_sections = []

    # ── One brief per team (people in several teams get one combined email) ──
    per_person = {}
    for t in teams:
        tab_out, ids = {}, []
        for tab, lst in cands.items():
            mine = [dict(c) for c in lst if c["platform"] in t["platforms"]]
            tasks, waiting = pick(mine, state, bc.MAX_TASKS_PER_TAB)
            for n, c in enumerate(tasks):
                c["id"] = f"{tab[0]}{n + 1}"
                shown[c["key"]] = c
            tab_out[tab] = (tasks, waiting)
            ids += tasks
        h = scope_headline(d, t["platforms"])
        words = claude_wording(f"Growth managers for {t['name']} ({', '.join(sorted(t['platforms']))})", h, ids)
        body = "".join(render_tab(tab, *tab_out[tab], words["actions"]) for tab in cands)
        notes = []
        st = [i for i in stretch if i["platform"] in t["platforms"]][:3]
        if st:
            notes.append("Plan may be the issue, not execution (target set >1.3× recent run-rate): "
                         + "; ".join(f"{e(i['name'])} on {e(i['platform'])} at {i['facts']['attainment_vs_pace']}" for i in st))
        waiting_data = sorted(excluded & t["platforms"])
        if waiting_data:
            notes.append(f"Awaiting data this month: {e(', '.join(waiting_data))} (not counted as a miss).")
        good = improved(state, flagged_keys, t["platforms"])
        if good:
            notes.append("✅ No longer flagged since raised: " + "; ".join(
                f"{e(v['name'])} on {e(v['platform'])} ({e(v['tab'])})" for v in good))
        section = {"team": t, "headline": h, "intro": words.get("intro", ""), "body": body, "notes": notes}
        team_sections.append(section)
        for em in t["emails"]:
            per_person.setdefault(em.lower(), []).append(section)

    for em, secs in per_person.items():
        if len(secs) == 1:
            sc = secs[0]
            title = f"Morning brief · {sc['team']['name']}"
            out = render_email(title, sub, sc["intro"], render_headline(sc["headline"]), sc["body"], sc["notes"])
        else:  # combined email
            title = "Morning brief · " + " + ".join(sc["team"]["name"] for sc in secs)
            body = "".join(f'<h3 style="{CSS["h3"]}">{e(sc["team"]["name"])}</h3>{render_headline(sc["headline"])}'
                           + (f"<p>{e(sc['intro'])}</p>" if sc["intro"] else "") + sc["body"]
                           + ("<ul style='font-size:13px'>" + "".join(f"<li>{n}</li>" for n in sc["notes"]) + "</ul>" if sc["notes"] else "")
                           for sc in secs)
            out = render_email(title, sub, "", "", body, [])
        fn = f"{bc.OUTPUT_DIR}/{em.split('@')[0]}.html"
        open(fn, "w", encoding="utf-8").write(out)
        send([em], f"{title} · {TODAY.strftime('%d %b')}", out)

    # ── Master brief ──
    mh = {
        "attainment_vs_pace": pct(s["overall_attainment_pct"]), "revenue_mtd": inr(s["total_actual_revenue"]),
        "revenue_gap": inr(s["total_revenue_gap"]), "roas_mtd": f"{s['roas_mtd']:.2f}",
        "roas_last_month": f"{s['roas_lfm']:.2f}", "actual_discount": pct(s["actual_discount_pct"]),
        "guideline_discount": pct(s["guideline_discount_pct"]),
    }
    top = []
    for tab, lst in cands.items():
        items = [dict(c) for c in lst]
        tasks, waiting = pick(items, state, bc.MASTER_MAX_PER_TAB)
        for n, c in enumerate(tasks):
            c["id"] = f"m{tab[0]}{n + 1}"
        top.append((tab, tasks, waiting))
    mwords = claude_wording("Head of marketplaces (whole business, all platforms)",
                            {**mh, "planned_sku_rows_flagged": {t: len(l) for t, l in cands.items()}},
                            [c for _, ts, _ in top for c in ts])
    body = f'<h2 style="{CSS["h2"]}">Business-wide top items</h2>'
    body += "".join(render_tab(tab, ts, ws, mwords["actions"]) for tab, ts, ws in top)
    body += f'<h2 style="{CSS["h2"]}">What each team received</h2>'
    for sc in team_sections:
        body += (f'<h3 style="{CSS["h3"]}">{e(sc["team"]["name"])} '
                 f'<span style="{CSS["muted"]}">→ {e(", ".join(sc["team"]["emails"]))}</span></h3>'
                 f'{render_headline(sc["headline"])}{sc["body"]}')
    unplanned = [r for r in d["sku_detail"] if (r.get("Prorated Planned SP Revenue") or 0) == 0 and r["MTD Actual SP Revenue"] > 0]
    cat_mrp = {r["Category"]: r["Actual_MRP_Revenue"] for r in d["cat_summary"]}
    sysd = sorted([r for r in d["discount_by_cat"] if r.get("Discount_Flag") == "critical"],
                  key=lambda r: -cat_mrp.get(r["Category"], 0))[:5]
    big = sorted([r for r in d["sku_detail"] if (r.get("LM Revenue Growth %") or 0) <= -20
                  and r["Platform"] not in excluded], key=lambda r: -r["MTD Actual SP Revenue"])[:5]
    notes = [
        f"Flag counts on the dashboard: {s['critical_revenue_skus']} critical / {s['warning_revenue_skus']} warning revenue rows. "
        f"{len(unplanned)} rows are selling with no plan ({inr(sum(r['MTD Actual SP Revenue'] for r in unplanned))} MTD) and show as 0% attainment; "
        f"they are left out of tasks.",
    ]
    if sysd:
        notes.append("Category-wide over-discounting (raise with category owners): " + "; ".join(
            f"{e(r['Category'])} {pct(r['Actual_Discount_Pct'])} vs guideline {pct(r['Guideline_Discount_Pct'])}" for r in sysd))
    if stretch:
        notes.append("Target Stretch misses, likely a planning issue: " + "; ".join(
            f"{e(i['name'])} on {e(i['platform'])} ({i['facts']['revenue_gap']})" for i in stretch[:5]))
    if big:
        notes.append("Shrinking vs last month (≥20% down) among top sellers: " + "; ".join(
            f"{e(r.get('Short Name') or r['SKU'])} on {e(r['Platform'])} ({spct(r['LM Revenue Growth %'])})" for r in big))
    if excluded:
        notes.append(f"Awaiting data this month: {e(', '.join(sorted(excluded)))}.")
    if unmapped["count"]:
        notes.append(f"Data hygiene: {unmapped['count']} ad SKUs have no matching sales row (Unmapped Category), "
                     f"{inr(unmapped['spend'])} spend MTD.")
    title = "Morning brief · All platforms (master)"
    out = render_email(title, sub, mwords.get("intro", ""), render_headline(mh), body, notes)
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
                        "metric_at_raise": prev["metric_at_raise"] if prev else c["metric"], "metric_last": c["metric"]}
        os.makedirs(os.path.dirname(bc.STATE_FILE), exist_ok=True)
        with open(bc.STATE_FILE, "w") as f:
            json.dump(state, f, indent=1, ensure_ascii=False)
        log.info(f"  Memory saved: {len(items)} open items")
    log.info("✅ Done")


if __name__ == "__main__":
    main()
