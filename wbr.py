"""
wbr.py
──────
Weekly Business Review agent.

Stages (env WBR_STAGE):
  queries   – emails each manager whose WBR is QUERY_DAYS_BEFORE days away: 3–6 data-backed questions
  reminder  – nudges managers with a WBR in 1–3 days who have not replied yet (same email thread)
  pack      – on WBR day: reads the replies, judges them against the review-period data,
              and sends the head one WBR pack (executive summary + one page per manager)

Mode (env WBR_MODE): dry (default, previews only) | test (everything to TEST_RECIPIENT) | send
Optional: WBR_DATE=YYYY-MM-DD (act as if that is the WBR date), WBR_MANAGER=key, WBR_FORCE=yes
Env: ANTHROPIC_API_KEY, GMAIL_USER, GMAIL_APP_PASSWORD, TEST_RECIPIENT
"""

import os
import re
import json
import html
import email
import imaplib
import smtplib
import logging
import datetime as dt
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import make_msgid, formataddr, parseaddr

import config
import wbr_config as wc
import snapshots as snap
from brief import inr, pct, spct, e, CSS, TD, TH, table, _att_style, _roas_style, _disc_style

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("wbr")

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
NOW = dt.datetime.now(IST)
TODAY = NOW.date()
STAGE = os.environ.get("WBR_STAGE", "").strip().lower()
MODE = os.environ.get("WBR_MODE", "dry").strip().lower()
if MODE not in ("dry", "test", "send"):
    MODE = "dry"
FORCE = os.environ.get("WBR_FORCE") == "yes"
ONLY = os.environ.get("WBR_MANAGER", "").strip().lower()
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
STATE_DIR = wc.STATE_DIR + ("_test" if MODE == "test" else "")


# ══════════════════════════════════════════════
#  Dates
# ══════════════════════════════════════════════
def working_saturday(d):
    if d.weekday() != 5:
        return False
    if d.isoformat() in getattr(wc, "SATURDAYS_OFF", []):
        return False
    if d.isoformat() in getattr(wc, "EXTRA_WORKING_SATURDAYS", []):
        return True
    return ((d.day - 1) // 7 + 1) in wc.WORKING_SATURDAYS


def review_period(wbr):
    """Monday WBR: Sat–Fri ending the Friday before.
    Tuesday WBR: Sun–Sat ending the Saturday before if it was a working Saturday, else Fri–Thu."""
    if wbr.weekday() == 0:
        end = wbr - dt.timedelta(days=3)
    elif wbr.weekday() == 1:
        sat = wbr - dt.timedelta(days=3)
        end = sat if working_saturday(sat) else sat - dt.timedelta(days=2)
    else:
        raise ValueError("WBRs are on Monday or Tuesday")
    return end - dt.timedelta(days=6), end


def deadline_day(wbr):
    d = wbr - dt.timedelta(days=1)
    while d.weekday() >= 5:  # replies by the last weekday before the WBR
        d -= dt.timedelta(days=1)
    return d


def fmt_d(d):
    return d.strftime("%a %d %b").replace(" 0", " ")


def fmt_t(t):
    h, m = map(int, t.split(":"))
    return f"{(h - 1) % 12 + 1}:{m:02d} {'am' if h < 12 else 'pm'}".replace(":00 ", " ")


def fmt_range(a, b):
    return f"{a.strftime('%d %b').lstrip('0')} – {b.strftime('%d %b').lstrip('0')}"


def next_wbr(m, frm):
    wd = DAYS.index(m["day"])
    d = frm
    while d.weekday() != wd:
        d += dt.timedelta(days=1)
    return d


# ══════════════════════════════════════════════
#  Data
# ══════════════════════════════════════════════
def load_current():
    with open(f"{config.PROCESSED_DIR}/index.json") as f:
        idx = json.load(f)
    month = idx["latest"]
    d = {}
    for n in ("summary", "prorate_info", "discount_sku", "sku_detail", "cat_plat_summary", "ads_sku_detail"):
        with open(f"{config.PROCESSED_DIR}/{month}/{n}.json") as f:
            d[n] = json.load(f)
    d["month"] = month
    return month, d


def resolve_platforms(all_plats):
    named = {p for m in wc.MANAGERS for p in m["platforms"] if p != "REST"}
    rest = set(all_plats) - named - set(wc.NEVER_IN_WBR)
    for m in wc.MANAGERS:
        ps = {p for p in m["platforms"] if p != "REST"}
        if "REST" in m["platforms"]:
            ps |= rest
        m["plats"] = sorted(ps - set(wc.NEVER_IN_WBR))
        # business-snapshot accounts: each named platform, plus one combined table for the "REST" platforms
        m["accounts"] = [{"name": p, "plats": [p]} for p in m["platforms"] if p not in ("REST",) and p in all_plats]
        if "REST" in m["platforms"] and rest:
            m["accounts"].append({"name": wc.OTHER_ACCOUNTS_LABEL, "plats": sorted(rest)})


def metric_labels(account):
    qc = all(p in wc.QUICK_COMMERCE for p in account["plats"])
    return [("Availability" if (x == "SLA" and qc) else x) for x in wc.SNAPSHOT_METRICS]


class Week:
    """Per-platform totals for a window, aggregated to platform and category × platform."""

    def __init__(self, hist, platforms, start, end, guide, names):
        self.start, self.end, self.plat, self.cp, self.sku, self.till = start, end, {}, {}, {}, {}
        self.since = {}
        for p in platforms:
            rows, eff_start, eff_end, days, plan = hist.window(p, start, end)
            self.till[p], self.since[p] = eff_end, eff_start
            if not rows:
                continue
            for sku, r in rows.items():
                cat = r[0]
                v = {"rev": r[1], "qty": r[2], "mrp": r[3], "spend": r[5], "ad_sales": r[6], "clicks": r[7],
                     "gross": r[8], "plan": plan.get(sku, 0), "rate": r[1] / days if days else 0}
                g = guide.get((p, sku))
                v["gmrp"] = r[3] if g is not None else 0
                v["gsum"] = (g or 0) * r[3]
                for key in (("plat", p), ("cp", (cat, p))):
                    tgt = getattr(self, key[0]).setdefault(key[1], {k: 0 for k in v})
                    for k in v:
                        tgt[k] += v[k]
                self.sku[(p, sku)] = {**v, "cat": cat, "name": names.get((p, sku), sku)}

    @staticmethod
    def metrics(v):
        if not v:
            return {}
        disc = (v["mrp"] - v["rev"]) / v["mrp"] * 100 if v["mrp"] else None
        guide = v["gsum"] / v["gmrp"] if v["gmrp"] else None
        return {"rev": v["rev"], "plan": v["plan"], "rate": v.get("rate", 0), "gap": v["rev"] - v["plan"] if v["plan"] else None,
                "att": v["rev"] / v["plan"] * 100 if v["plan"] else None,
                "spend": v["spend"], "roas": v["ad_sales"] / v["spend"] if v["spend"] else None,
                "disc": disc, "guide": guide,
                "disc_vs_guide": disc - guide if disc is not None and guide is not None else None}

    def total(self, plats):
        acc = None
        for p in plats:
            v = self.plat.get(p)
            if not v:
                continue
            acc = dict(v) if acc is None else {k: acc[k] + v[k] for k in acc}
        return acc


def covered(wk, p):
    a, b = wk.since.get(p), wk.till.get(p)
    if not a or not b:
        return "no data"
    if a == wk.start and b == wk.end:
        return "full week"
    return fmt_range(a, b)


def wow(cur, prev):
    """Change in daily run-rate vs the previous week (fair even when a week is only partly covered)."""
    if not cur or not prev or not prev.get("rate"):
        return None
    return round((cur["rate"] / prev["rate"] - 1) * 100, 1) + 0.0


def facts_for(cat, p, wk, pw):
    """Human-readable facts for one category × platform (also used to judge answers)."""
    m, mp = Week.metrics(wk.cp.get((cat, p))), Week.metrics(pw.cp.get((cat, p)))
    skus = sorted([(k, v) for k, v in wk.sku.items() if k[0] == p and v["cat"] == cat and v["plan"]],
                  key=lambda kv: kv[1]["rev"] - kv[1]["plan"])[:2]
    f = {
        "week": covered(wk, p) if covered(wk, p) != "full week" else fmt_range(wk.start, wk.end),
        "week_revenue": inr(m.get("rev")), "week_target": inr(m.get("plan")),
        "attainment": pct(m.get("att")), "gap": inr(m.get("gap")),
        "vs_previous_week": spct(wow(m, mp)),
        "ad_spend": inr(m.get("spend")) if m.get("spend") else "no ads",
        "roas": f"{m['roas']:.2f}" if m.get("roas") else "–",
        "roas_previous_week": f"{mp['roas']:.2f}" if mp.get("roas") else "–",
        "discount": pct(m.get("disc")), "guideline": pct(m.get("guide")),
        "discount_vs_guideline": "–" if m.get("disc_vs_guide") is None else f"{round(m['disc_vs_guide'], 1) + 0.0:+.1f} pp".replace("-", "−").replace("+0.0", "0.0"),
        "sku_drivers": "; ".join(f"{v['name']} ({inr(v['rev'] - v['plan'])})" for _, v in skus) or "–",
    }
    return f, m, mp


# ══════════════════════════════════════════════
#  Claude
# ══════════════════════════════════════════════
def claude(system, payload, max_tokens=3000):
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log.warning("  No ANTHROPIC_API_KEY set; using rule-based wording")
        return None
    try:
        import anthropic
        msg = anthropic.Anthropic().messages.create(
            model=os.environ.get("WBR_MODEL", wc.MODEL), max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}])
        text = "".join(b.text for b in msg.content if b.type == "text").strip()
        text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        out = json.loads(text)
        log.info("  ✓ Claude OK")
        return out
    except Exception as ex:
        log.warning(f"  Claude failed ({ex}); using rule-based wording")
        return None


QUERY_PROMPT = """You draft pre-WBR questions that the Head of Marketplaces at Nat Habit (an Indian personal-care brand) sends to a growth manager.

For each item you get the data facts. Write one question per item: 1–2 sentences, quoting the 2–3 most telling figures exactly as given, ending with a clear ask. Tone: direct, respectful, curious, never accusatory. Don't ask for reason/action/date in every question; the email already asks for that.

Reply with ONLY JSON: {"questions": {"<id>": "..."}}. No markdown fences."""

EVAL_PROMPT = """You prepare the Head of Marketplaces at Nat Habit for a Weekly Business Review with one growth manager.

You get: the questions sent before the WBR (with the data at that time), the same metrics for the official review period, and the manager's email reply (may be empty, or answer only some questions).

For each question:
- "answer": what the manager said, in one sentence (or "" if not answered).
- "verdict": one of "Aligned", "Partly aligned", "Not aligned", "No answer".
  Aligned = the reason fits the data AND the action fits the root cause AND there is something concrete (what, by when).
  Partly aligned = plausible but vague, or right reason with a weak/missing action, or no timeline.
  Not aligned = the explanation contradicts the data (e.g. says discount was reduced but discount vs guideline rose), or the action does not address the cause.
- "why": one sentence citing the specific figures that support the verdict.
- "ask": one sharp question for the head to ask in the WBR.
Levers by cause: units gap → stock, buy-box, listing visibility, ad coverage; discount above guideline with weak volume → pricing/coupons/deals; ROAS drop → targeting, bids, creative, wasted spend; plans far above run-rate → re-planning.
You also get the manager's business snapshot for the month (Target and Estimated achievement for Primary, Secondary, Discounting %, Spend, SLA/Availability) next to the dashboard's plan and run-rate, with flags. Weigh it: an estimate well above run-rate with no action to close the gap is optimistic; targets that differ from the dashboard plan need explaining.
Also give "summary" (one sentence on how this manager's week went, how convincing the answers were, and whether the month estimate is credible) and "press" (the 2 most important points to press on, each max 15 words).
Use only the figures given, exactly as written. Actions take a few days to show; don't penalise a sound action just because results haven't moved yet.

Reply with ONLY JSON: {"questions": {"<id>": {"answer": "", "verdict": "", "why": "", "ask": ""}}, "summary": "", "press": ["", ""]}. No markdown fences."""


# ══════════════════════════════════════════════
#  Email (send + read replies)
# ══════════════════════════════════════════════
def send_mail(to, cc, subject, body, in_reply_to=None, label=""):
    msg_id = make_msgid(domain=(os.environ.get("GMAIL_USER", "x@nathabit.in").split("@")[1]))
    preview = f"{wc.OUTPUT_DIR}/{label or re.sub('[^a-z0-9]+', '_', subject.lower())[:60]}.html"
    os.makedirs(wc.OUTPUT_DIR, exist_ok=True)
    open(preview, "w", encoding="utf-8").write(body)
    if MODE == "dry":
        return msg_id
    if MODE == "test":
        subject = f"[TEST → {', '.join(to + cc)}] {subject}"
        to, cc = [os.environ["TEST_RECIPIENT"]], []
    user, pw = os.environ["GMAIL_USER"], os.environ["GMAIL_APP_PASSWORD"]
    m = MIMEMultipart("alternative")
    m["Subject"], m["From"], m["To"] = subject, formataddr((wc.SENDER_NAME, user)), ", ".join(to)
    if cc:
        m["Cc"] = ", ".join(cc)
    m["Message-ID"] = msg_id
    if in_reply_to:
        m["In-Reply-To"] = m["References"] = in_reply_to
    m.attach(MIMEText(body, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pw)
        s.sendmail(user, to + cc, m.as_string())
    log.info(f"  Sent → {', '.join(to + cc)}")
    return msg_id


QUOTE_START = re.compile(r"^(On .{5,200}wrote:|-{2,}\s*Original Message|From: .+|Sent from my )", re.I)


def clean_reply(text):
    out = []
    for line in text.replace("\r", "").split("\n"):
        if QUOTE_START.match(line.strip()):
            break
        if line.strip().startswith(">"):
            continue
        out.append(line)
    return "\n".join(out).strip()


def html_to_text(h):
    h = re.sub(r"(?is)<(script|style).*?</\1>", "", h)
    h = re.sub(r"(?i)</t[dh]>", " | ", h)
    h = re.sub(r"(?i)</tr>|<br\s*/?>|</p>|</div>|</h\d>", "\n", h)
    t = html.unescape(re.sub(r"<[^>]+>", " ", h))
    t = re.sub(r"[ \t\xa0]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n", t).strip()


def full_text(msg):
    """Whole message including quoted parts, with table cells kept apart (for the snapshot table)."""
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            payload = part.get_payload(decode=True)
            if payload:
                return html_to_text(payload.decode(part.get_content_charset() or "utf-8", "replace"))
    return body_text(msg)


def body_text(msg):
    plain, htm = None, None
    for part in msg.walk():
        ct = part.get_content_type()
        if part.get_content_maintype() == "multipart" or part.get("Content-Disposition", "").startswith("attachment"):
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        txt = payload.decode(part.get_content_charset() or "utf-8", "replace")
        if ct == "text/plain" and plain is None:
            plain = txt
        elif ct == "text/html" and htm is None:
            htm = txt
    if plain is None and htm:
        h = re.sub(r"(?is)<(blockquote|div class=\"gmail_quote).*", "", htm)
        plain = html.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", h)))
    return plain or ""


def fetch_replies(tag, allowed, since, own_ids=()):
    """Replies whose subject carries `tag`, from allowed addresses only (never the agent's own emails)."""
    user, pw = os.environ.get("GMAIL_USER"), os.environ.get("GMAIL_APP_PASSWORD")
    if not user or not pw:
        log.warning("  No Gmail credentials; cannot read replies")
        return []
    out = []
    try:
        im = imaplib.IMAP4_SSL("imap.gmail.com")
        im.login(user, pw)
        box = '"[Gmail]/All Mail"'
        if im.select(box, readonly=True)[0] != "OK":
            im.select("INBOX", readonly=True)
        typ, data = im.search(None, "SINCE", since.strftime("%d-%b-%Y"), "SUBJECT", f'"{tag}"')
        for num in (data[0].split() if typ == "OK" else []):
            typ, msgd = im.fetch(num, "(RFC822)")
            if typ != "OK":
                continue
            msg = email.message_from_bytes(msgd[0][1])
            frm = parseaddr(msg.get("From", ""))[1].lower()
            if frm not in allowed or (msg.get("Message-ID") or "").strip() in own_ids:
                continue
            text = clean_reply(body_text(msg))
            if text:
                out.append({"from": frm, "date": msg.get("Date", ""), "text": text[:6000],
                            "full": full_text(msg)[:15000]})
        im.logout()
    except Exception as ex:
        log.warning(f"  Could not read replies for {tag}: {ex}")
    return out


# ══════════════════════════════════════════════
#  State
# ══════════════════════════════════════════════
def state_path(wbr, key):
    return f"{STATE_DIR}/{wbr.isoformat()}_{key}.json"


def load_state(wbr, key):
    try:
        with open(state_path(wbr, key)) as f:
            return json.load(f)
    except Exception:
        return None


def save_state(wbr, key, st):
    if MODE == "dry":
        return
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(state_path(wbr, key), "w") as f:
        json.dump(st, f, indent=1, ensure_ascii=False, default=str)


def tag_for(wbr, key):
    return f"WBR-{wbr.strftime('%Y%m%d')}-{key}"


# ══════════════════════════════════════════════
#  Stage 1: queries
# ══════════════════════════════════════════════
def pick_questions(m, wk, pw, brief_state):
    plats = m["plats"]
    cands = []
    for (cat, p), v in wk.cp.items():
        if p not in plats or v["plan"] < wc.MIN_WEEK_TARGET:
            continue
        mm = Week.metrics(v)
        if mm["gap"] is not None and mm["gap"] < 0:
            cands.append(("revenue", cat, p, mm["gap"]))
    cands.sort(key=lambda c: c[3])
    chosen = cands[: wc.REVENUE_QUESTIONS]
    used = {(c[1], c[2]) for c in chosen}
    # ads: biggest week-on-week ROAS drop with meaningful spend
    ads = []
    for (cat, p), v in wk.cp.items():
        if p not in plats or (cat, p) in used or v["spend"] < wc.MIN_WEEK_AD_SPEND:
            continue
        mm, mp = Week.metrics(v), Week.metrics(pw.cp.get((cat, p)))
        if mm.get("roas") and mp.get("roas") and (mp["roas"] - mm["roas"]) / mp["roas"] * 100 >= wc.ROAS_DROP_FOR_QUESTION:
            ads.append(("ads", cat, p, -v["spend"] * (mp["roas"] - mm["roas"])))
    ads.sort(key=lambda c: c[3])
    chosen += ads[:1]
    used |= {(c[1], c[2]) for c in ads[:1]}
    # discount: most rupees given beyond guideline this week
    disc = []
    for (cat, p), v in wk.cp.items():
        if p not in plats or (cat, p) in used:
            continue
        mm = Week.metrics(v)
        if mm.get("disc_vs_guide") is not None and mm["disc_vs_guide"] >= wc.DISCOUNT_PP_FOR_QUESTION:
            disc.append(("discount", cat, p, -v["mrp"] * mm["disc_vs_guide"] / 100))
    disc.sort(key=lambda c: c[3])
    chosen += disc[:1]
    used |= {(c[1], c[2]) for c in disc[:1]}
    # persistent: daily-brief items raised 2+ times without improving
    for k, v in (brief_state.get("items") or {}).items():
        if len(chosen) >= wc.MAX_QUESTIONS:
            break
        if v.get("platform") in plats and v.get("times_raised", 1) >= 2:
            chosen.append(("persistent", v["tab"], v["platform"], v["name"]))
            break
    return chosen[: wc.MAX_QUESTIONS]


def fallback_question(kind, f, cat, p):
    if kind == "revenue":
        return (f"{cat} on {p} did {f['week_revenue']} against a {f['week_target']} target ({f['attainment']}, gap {f['gap']}), "
                f"{f['vs_previous_week']} vs the previous week. What is driving the gap?")
    if kind == "ads":
        return (f"Ads for {cat} on {p}: ROAS fell to {f['roas']} from {f['roas_previous_week']} the previous week on "
                f"{f['ad_spend']} spend. What changed, and is the spend still justified?")
    if kind == "discount":
        return (f"{cat} on {p} ran a {f['discount']} discount against a {f['guideline']} guideline "
                f"({f['discount_vs_guideline']}). Why, and what is it buying us in volume?")
    return f"{cat} on {p} has been flagged repeatedly in the daily brief without improving. What is blocking it?"


def run_queries(wbr_target, managers, hist, guide, names, brief_state):
    for m in managers:
        wbr = wbr_target or next_wbr(m, TODAY + dt.timedelta(days=wc.QUERY_DAYS_BEFORE))
        if not wbr_target and (wbr - TODAY).days != wc.QUERY_DAYS_BEFORE:
            continue
        if load_state(wbr, m["key"]) and not FORCE:
            log.info(f"  {m['name']}: questions already sent for {wbr}; skipping")
            continue
        # most recent 7 days available, per platform
        latest = max((hist.latest_till(p, TODAY.strftime("%Y-%m")) or 0) for p in m["plats"]) or TODAY.day - 1
        end = min(TODAY - dt.timedelta(days=1), dt.date(TODAY.year, TODAY.month, max(1, latest)))
        start = end - dt.timedelta(days=6)
        wk = Week(hist, m["plats"], start, end, guide, names)
        pw = Week(hist, m["plats"], start - dt.timedelta(days=7), end - dt.timedelta(days=7), guide, names)
        picks = pick_questions(m, wk, pw, brief_state)
        if not picks:
            log.info(f"  {m['name']}: nothing material to ask; skipping")
            continue
        qs = []
        for i, (kind, cat, p, _) in enumerate(picks, 1):
            if kind == "persistent":
                qs.append({"id": f"Q{i}", "kind": kind, "category": cat, "platform": p,
                           "facts": {"item": _, "note": "raised 2+ times in the daily brief without improving"}})
                continue
            f, _, _ = facts_for(cat, p, wk, pw)
            qs.append({"id": f"Q{i}", "kind": kind, "category": cat, "platform": p, "facts": f})
        out = claude(QUERY_PROMPT, {"manager": m["name"], "items": qs}) or {}
        for q in qs:
            q["text"] = (out.get("questions") or {}).get(q["id"]) or (
                fallback_question(q["kind"], q["facts"], q["category"], q["platform"]) if q["kind"] != "persistent"
                else f"{q['facts']['item']} on {q['platform']} has been flagged repeatedly in the daily brief without improving. What is blocking it?")
        rp = review_period(wbr)
        dl = deadline_day(wbr)
        subject = f"WBR prep · {fmt_d(wbr)} {fmt_t(m['time'])} · {m['name']} [{tag_for(wbr, m['key'])}]"
        body = render_query_email(m, wbr, rp, dl, qs, wk)
        mid = send_mail(m["to"], m["cc"], subject, body, label=f"query_{m['key']}")
        save_state(wbr, m["key"], {"wbr": wbr, "manager": m["key"], "sent_at": NOW.isoformat(), "message_id": mid,
                                   "subject": subject, "query_window": [start, end], "questions": qs, "own_ids": [mid],
                                   "snapshot_accounts": [{"name": a["name"], "plats": a["plats"], "metrics": metric_labels(a)}
                                                         for a in m["accounts"]],
                                   "reminder_sent": False, "pack_sent": False})


def render_snapshot_form(m, wbr):
    month = wbr.strftime("%B %Y")
    th = "padding:6px 10px;border:1px solid #d0d7de;background:#f6f8fa;font-size:12px;text-align:left"
    td = "padding:6px 10px;border:1px solid #d0d7de;font-size:13px;min-width:110px"
    out = (f'<h3 style="{CSS["h3"]};margin-top:22px">Business snapshot · {month}</h3>'
           f'<p style="margin:0 0 8px">Please fill in the table{"s" if len(m["accounts"]) > 1 else ""} below for '
           f'<b>{month}</b> and include {"them" if len(m["accounts"]) > 1 else "it"} in your reply '
           f'(copy into your reply, or type into the quoted table). ₹ values in lakh.</p>')
    for a in m["accounts"]:
        rows = "".join(f'<tr><td style="{td}"><b>{e(x)}</b> <span style="color:#59636e">({e(wc.SNAPSHOT_UNITS[x])})</span></td>'
                       f'<td style="{td}">&nbsp;</td><td style="{td}">&nbsp;</td></tr>' for x in metric_labels(a))
        title = a["name"] + (f' <span style="color:#59636e;font-weight:normal">({e(", ".join(a["plats"]))})</span>'
                             if a["name"] == wc.OTHER_ACCOUNTS_LABEL else "")
        out += (f'<table style="border-collapse:collapse;margin:0 0 12px">'
                f'<tr><th style="{th}">{title}</th><th style="{th}">Target</th><th style="{th}">Estimated achievement</th></tr>'
                f'{rows}</table>')
    return out


def render_query_email(m, wbr, rp, dl, qs, wk):
    till = ", ".join(f"{p} till {fmt_d(d)}" for p, d in wk.till.items() if d) or "latest available"
    items = "".join(
        f'<tr><td style="{TD};vertical-align:top;font-weight:bold;color:#0b5394">{q["id"]}</td>'
        f'<td style="padding:6px 8px;border-bottom:1px solid #eaeef2;font-size:14px;line-height:1.45">{e(q["text"])}</td></tr>'
        for q in qs)
    return (f'<div style="{CSS["body"]}">'
            f'<p>Hi {e(m["name"])},</p>'
            f'<p>Ahead of our WBR on <b>{fmt_d(wbr)} at {fmt_t(m["time"])}</b> (review period {fmt_range(*rp)}), '
            f'I’d like your view on the points below. Please <b>reply to this email by {fmt_d(dl)}, {wc.REPLY_DEADLINE}</b>.</p>'
            f'<p>For each question, please cover: <b>(1) the reason</b>, <b>(2) the action you have taken or will take</b>, '
            f'and <b>(3) when you expect it to show in the numbers</b>. Short answers are fine; just keep the Q numbers. '
            f'Please also fill in the business snapshot table at the end.</p>'
            f'<table style="border-collapse:collapse;width:100%">{items}</table>'
            + render_snapshot_form(m, wbr) +
            f'<p style="{CSS["muted"]};margin-top:14px">Figures are for {fmt_range(wk.start, wk.end)} ({e(till)}), '
            f'from the Category Intelligence dashboard. Targets are prorated to the days covered.</p>'
            f'<p>Thanks,<br>{e(wc.SENDER_NAME)}</p>'
            f'<p style="{CSS["muted"]}">Prepared automatically from the dashboard on {e(wc.SENDER_NAME)}’s behalf. '
            f'Your reply goes to {e(wc.SENDER_NAME)}.</p></div>')


# ══════════════════════════════════════════════
#  Stage 2: reminder
# ══════════════════════════════════════════════
def run_reminders(wbr_target, managers):
    for m in managers:
        wbr = wbr_target or next_wbr(m, TODAY + dt.timedelta(days=1))
        days = (wbr - TODAY).days
        if not wbr_target and not (1 <= days <= 3):
            continue
        st = load_state(wbr, m["key"])
        if not st or (st.get("reminder_sent") and not FORCE):
            continue
        allowed = {a.lower() for a in m["to"] + m["cc"]}
        if MODE == "test":
            allowed.add(os.environ.get("TEST_RECIPIENT", "").lower())
        if fetch_replies(tag_for(wbr, m["key"]), allowed, dt.date.fromisoformat(st["sent_at"][:10]),
                         set(st.get("own_ids") or [st.get("message_id")])):
            log.info(f"  {m['name']}: already replied; no reminder")
            continue
        dl = deadline_day(wbr)
        body = (f'<div style="{CSS["body"]}"><p>Hi {e(m["name"])},</p>'
                f'<p>A quick reminder on the WBR questions below, ahead of {fmt_d(wbr)} at {fmt_t(m["time"])}. '
                f'A short reply by {fmt_d(dl)}, {wc.REPLY_DEADLINE} would really help me prepare.</p>'
                f'<p>Thanks,<br>{e(wc.SENDER_NAME)}</p></div>')
        rid = send_mail(m["to"], m["cc"], "Re: " + st["subject"], body, in_reply_to=st.get("message_id"),
                        label=f"reminder_{m['key']}")
        st["reminder_sent"] = NOW.isoformat()
        st["own_ids"] = list(set((st.get("own_ids") or [st.get("message_id")]) + [rid]))
        save_state(wbr, m["key"], st)


# ══════════════════════════════════════════════
#  Business snapshot table (Target / Estimated achievement)
# ══════════════════════════════════════════════
NUM = re.compile(r"-?\d[\d,]*\.?\d*")


def to_num(cell):
    """'₹1.2 Cr' → 120 (lakh); '85%' → 85; '45 L' → 45; '4,50,000' → 4.5 (lakh, if clearly rupees)."""
    c = (cell or "").replace("₹", "").strip().lower()
    mt = NUM.search(c)
    if not mt:
        return None
    v = float(mt.group().replace(",", ""))
    if "cr" in c:
        v *= 100
    elif re.search(r"\bk\b|thousand", c):
        v /= 100
    elif v >= 100000 and "%" not in c:  # plain rupees typed in full
        v /= 100000
    return round(v, 2)


def parse_snapshot(text, accounts):
    """Reads 'Metric (unit) | target | estimate |' rows under each account header. The first filled
    value wins (the reply sits above the quoted, blank original)."""
    out = {a["name"]: {} for a in accounts}
    names = {a["name"].lower(): a["name"] for a in accounts}
    cur = None
    for line in text.split("\n"):
        cells = [c.strip() for c in line.split("|")]
        if len(cells) < 2:
            continue
        head = cells[0].lower()
        if "target" in line.lower() and "estimated" in line.lower():
            cur = next((v for k, v in names.items() if head.startswith(k)), cur)
            continue
        if not cur:
            continue
        a = next(x for x in accounts if x["name"] == cur)
        metric = next((mm for mm in a["metrics"] if head.startswith(mm.lower())), None)
        if not metric or metric in out[cur]:
            continue
        t, est = to_num(cells[1]) if len(cells) > 1 else None, to_num(cells[2]) if len(cells) > 2 else None
        if t is not None or est is not None:
            out[cur][metric] = {"target": t, "estimate": est}
    return out


SNAPSHOT_PROMPT = """Extract a growth manager's business-snapshot figures from their email reply.
You get the accounts and metrics that were asked for, and the reply text (tables may appear as 'cell | cell |' lines; the reply may also contain the quoted, empty original).
For each account and metric return the Target and Estimated achievement for the month as numbers: rupee values in lakh (1 Cr = 100), percentages as plain numbers. Use null when not given. Never guess.
Reply with ONLY JSON: {"snapshot": {"<account>": {"<metric>": {"target": number|null, "estimate": number|null}}}}. No markdown fences."""


def read_snapshot(replies, accounts):
    filled = {a["name"]: {} for a in accounts}
    for r in replies:
        got = parse_snapshot(r.get("full") or r["text"], accounts)
        for acc, mets in got.items():
            for k, v in mets.items():
                filled[acc].setdefault(k, v)
    missing = any(len(filled[a["name"]]) < len(a["metrics"]) for a in accounts)
    if replies and missing:
        out = claude(SNAPSHOT_PROMPT, {"accounts": accounts, "replies": [r.get("full") or r["text"] for r in replies]}, 2000)
        for acc, mets in ((out or {}).get("snapshot") or {}).items():
            for k, v in (mets or {}).items():
                if acc in filled and isinstance(v, dict) and k not in filled[acc]:
                    tv, ev = v.get("target"), v.get("estimate")
                    if isinstance(tv, (int, float)) or isinstance(ev, (int, float)):
                        filled[acc][k] = {"target": tv, "estimate": ev}
    return filled


def dashboard_view(d, plats, month_key):
    """What the dashboard says for the month, per snapshot metric (₹ in lakh)."""
    if d.get("month") != month_key:
        return {}
    cp = [r for r in d["cat_plat_summary"] if r["Platform"] in plats]
    plan_sp, plan_mrp = sum(r["Planned_SP_Revenue"] for r in cp), sum(r["Planned_MRP_Revenue"] for r in cp)
    act_sp, act_mrp = sum(r["Actual_SP_Revenue"] for r in cp), sum(r["Actual_MRP_Revenue"] for r in cp)
    ads = [r for r in d["ads_sku_detail"] if r["Platform"] in plats]
    ad_tgt, ad_mtd = sum(r.get("Ad_Spend_TGT") or 0 for r in ads), sum(r.get("Ad_Spend_MTD") or 0 for r in ads)
    info = [d["prorate_info"].get(p) or {} for p in plats]
    days = min((i.get("day") or 0) for i in info) if info else 0
    total = max((i.get("total") or 30) for i in info) if info else 30
    proj = (lambda x: x / days * total / 1e5) if days >= wc.MIN_DAYS_FOR_PROJECTION else (lambda x: None)
    return {
        "Secondary": {"plan": plan_sp / 1e5 if plan_sp else None, "projection": proj(act_sp), "days": days},
        "Discounting %": {"plan": (plan_mrp - plan_sp) / plan_mrp * 100 if plan_mrp else None,
                          "projection": (act_mrp - act_sp) / act_mrp * 100 if act_mrp else None, "days": days},
        "Spend": {"plan": ad_tgt / 1e5 if ad_tgt else None, "projection": proj(ad_mtd), "days": days},
    }


def snapshot_rows(m, filled, d, month_key):
    """Rows for the pack, with the dashboard cross-check and flags."""
    rows = []
    for a in m["accounts"]:
        dv = dashboard_view(d, a["plats"], month_key)
        for metric in metric_labels(a):
            v = (filled.get(a["name"]) or {}).get(metric) or {}
            t, est = v.get("target"), v.get("estimate")
            x = dv.get(metric) or {}
            flags = []
            is_pct = wc.SNAPSHOT_UNITS[metric] == "%"
            if t is not None and x.get("plan"):
                if (is_pct and abs(t - x["plan"]) >= 2) or (not is_pct and abs(t / x["plan"] - 1) * 100 > wc.FLAG_ESTIMATE_GAP_PCT):
                    flags.append("target differs from dashboard plan")
            if est is not None and x.get("projection"):
                if is_pct:
                    if abs(est - x["projection"]) >= 2:
                        flags.append("estimate differs from MTD actual")
                else:
                    gap = (est / x["projection"] - 1) * 100
                    if gap > wc.FLAG_ESTIMATE_GAP_PCT:
                        flags.append("estimate above run-rate")
                    elif gap < -wc.FLAG_ESTIMATE_GAP_PCT:
                        flags.append("estimate below run-rate")
            rows.append({"account": a["name"], "metric": metric, "unit": wc.SNAPSHOT_UNITS[metric], "target": t,
                         "estimate": est, "plan": x.get("plan"), "projection": x.get("projection"),
                         "days": x.get("days"), "flags": flags})
    return rows


def fv(x, unit):
    if x is None:
        return "–"
    return f"{x:.1f}%" if unit == "%" else f"₹{x:,.1f} L"


def render_snapshot_pack(rows, month_label):
    if not rows:
        return ""
    tr, last = "", None
    for r in rows:
        ratio = f"{r['estimate'] / r['target'] * 100:.0f}%" if r["target"] and r["estimate"] is not None and r["unit"] != "%" else "–"
        dash = []
        if r["plan"] is not None:
            dash.append(f"plan {fv(r['plan'], r['unit'])}")
        if r["projection"] is not None:
            dash.append(("MTD " if r["unit"] == "%" else "run-rate ") + fv(r["projection"], r["unit"]))
        flag = "".join(f'<span style="background:#fff8c5;color:#9a6700;padding:1px 6px;border-radius:10px;font-size:11px;'
                       f'margin-right:4px">⚠ {e(f)}</span>' for f in r["flags"])
        acc = f"<b>{e(r['account'])}</b>" if r["account"] != last else ""
        last = r["account"]
        tr += (f'<tr><td style="{TD}">{acc}</td><td style="{TD}">{e(r["metric"])}</td>'
               f'<td style="{TD}">{fv(r["target"], r["unit"])}</td><td style="{TD}">{fv(r["estimate"], r["unit"])}</td>'
               f'<td style="{TD}">{ratio}</td><td style="{TD};color:#59636e">{e(" · ".join(dash)) or "–"}</td>'
               f'<td style="padding:5px 8px;border-bottom:1px solid #eaeef2">{flag}</td></tr>')
    return (f'<h3 style="{CSS["h3"]}">Business snapshot · {e(month_label)}</h3>'
            + table(["Account", "Metric", "Target", "Est. achievement", "Est. vs target", "Dashboard view", ""], tr))


def secondary_total(rows):
    t = sum(r["target"] or 0 for r in rows if r["metric"] == "Secondary")
    est = sum(r["estimate"] or 0 for r in rows if r["metric"] == "Secondary")
    return (t, est) if t and est else (None, None)


# ══════════════════════════════════════════════
#  Stage 3: WBR pack
# ══════════════════════════════════════════════
VERDICT_STYLE = {"Aligned": "background:#dafbe1;color:#1a7f37", "Partly aligned": "background:#fff8c5;color:#9a6700",
                 "Not aligned": "background:#ffebe9;color:#cf222e", "No answer": "background:#eaeef2;color:#59636e",
                 "Not evaluated": "background:#eaeef2;color:#59636e"}


def vbadge(v):
    return f'<span style="{VERDICT_STYLE.get(v, VERDICT_STYLE["Not evaluated"])};padding:1px 7px;border-radius:10px;font-size:11px;font-weight:bold">{e(v)}</span>'


def run_pack(wbr, managers, hist, guide, names, s, d):
    todays = [m for m in managers if DAYS.index(m["day"]) == wbr.weekday()]
    if not todays:
        log.info(f"  No WBRs on {wbr}")
        return
    lock = f"{STATE_DIR}/{wbr.isoformat()}_pack.lock"
    if os.path.exists(lock) and not FORCE:
        log.info("  WBR pack already sent today; skipping")
        return
    todays.sort(key=lambda m: m["time"])
    rp = review_period(wbr)
    sections, summary_rows = [], []
    for m in todays:
        wk = Week(hist, m["plats"], rp[0], rp[1], guide, names)
        pw = Week(hist, m["plats"], rp[0] - dt.timedelta(days=7), rp[1] - dt.timedelta(days=7), guide, names)
        st = load_state(wbr, m["key"]) or {}
        qs = st.get("questions") or []
        allowed = {a.lower() for a in m["to"] + m["cc"]}
        if MODE == "test":
            allowed.add(os.environ.get("TEST_RECIPIENT", "").lower())
        replies = fetch_replies(tag_for(wbr, m["key"]), allowed, dt.date.fromisoformat(st["sent_at"][:10]),
                                set(st.get("own_ids") or [st.get("message_id")])) if st else []
        for q in qs:  # same metrics for the official review period
            if q["kind"] != "persistent":
                q["review_period_facts"] = facts_for(q["category"], q["platform"], wk, pw)[0]
        accounts = st.get("snapshot_accounts") or [{"name": a["name"], "plats": a["plats"], "metrics": metric_labels(a)}
                                                   for a in m["accounts"]]
        snap_rows = snapshot_rows(m, read_snapshot(replies, accounts), d, wbr.strftime("%Y-%m")) if replies else []
        snap_for_ai = [{k: (round(v, 1) if isinstance(v, float) else v) for k, v in r.items()} for r in snap_rows
                       if r["target"] is not None or r["estimate"] is not None]
        ev = None
        if qs:
            ev = claude(EVAL_PROMPT, {"manager": m["name"], "review_period": fmt_range(*rp), "questions": qs,
                                      "business_snapshot_for_the_month": snap_for_ai,
                                      "replies": [{"from": r["from"], "text": r["text"]} for r in replies]}, 4000)
        ev = ev or {}
        evq = ev.get("questions") or {}
        for q in qs:
            r = evq.get(q["id"]) or {}
            q["verdict"] = r.get("verdict") or ("No answer" if not replies else "Not evaluated")
            q["answer"], q["why"], q["ask"] = r.get("answer", ""), r.get("why", ""), r.get("ask", "")
            if not replies:  # no reply means no answer, whatever the model says
                q["verdict"], q["answer"] = "No answer", ""
            if q["verdict"] not in VERDICT_STYLE:
                q["verdict"] = "Not evaluated"
        tot, ptot = Week.metrics(wk.total(m["plats"])), Week.metrics(pw.total(m["plats"]))
        counts = {v: sum(1 for q in qs if q["verdict"] == v) for v in ("Aligned", "Partly aligned", "Not aligned", "No answer")}
        press = ev.get("press") or [f"{q['category']} on {q['platform']}" for q in qs if q["verdict"] in ("Not aligned", "No answer")][:2]
        summary_rows.append({"m": m, "tot": tot, "ptot": ptot, "counts": counts, "press": press,
                             "replied": bool(replies), "asked": bool(qs), "sec": secondary_total(snap_rows),
                             "snap_flags": sum(len(r["flags"]) for r in snap_rows)})
        sections.append(render_manager(m, wk, pw, tot, ptot, qs, replies, ev.get("summary", ""), s)
                        + (render_snapshot_pack(snap_rows, wbr.strftime("%B %Y")) if snap_rows else
                           f'<p style="{CSS["muted"]}">Business snapshot: not received.</p>'))
        if st:
            st["pack_sent"] = NOW.isoformat()
            st["replies"] = replies
            save_state(wbr, m["key"], st)
    body = render_pack_summary(wbr, rp, summary_rows, s) + "".join(sections)
    out = (f'<div style="{CSS["body"]};max-width:900px"><h1 style="{CSS["h1"]}">WBR pack · {fmt_d(wbr)}</h1>'
           f'<p style="{CSS["sub"]}">Review period {fmt_range(*rp)} · prepared {NOW.strftime("%d %b, %I:%M %p IST")}</p>'
           + body + "</div>")
    send_mail([wc.HEAD_EMAIL], [], f"WBR pack · {fmt_d(wbr)} · " + ", ".join(m["name"] for m in todays),
              out, label=f"pack_{wbr.isoformat()}")
    if MODE != "dry":
        os.makedirs(STATE_DIR, exist_ok=True)
        open(lock, "w").write(NOW.isoformat())


def render_pack_summary(wbr, rp, rows, s):
    tr = ""
    for r in rows:
        t, c = r["tot"], r["counts"]
        ans = ("no questions sent" if not r["asked"] else "no reply" if not r["replied"] else
               " ".join(f"{vbadge(k)}&nbsp;{n}" for k, n in c.items() if n) or "replied (not evaluated, see page)")
        tr += (f'<tr><td style="{TD}"><b>{e(r["m"]["name"])}</b><br><span style="color:#59636e;font-size:12px">'
               f'{fmt_t(r["m"]["time"])} · {e(", ".join(r["m"]["plats"][:4]))}{" +" + str(len(r["m"]["plats"]) - 4) if len(r["m"]["plats"]) > 4 else ""}</span></td>'
               f'<td style="{TD}">{inr(t.get("rev"))}</td>'
               f'<td style="{TD}{_att_style(t.get("att"), s)}">{pct(t.get("att"))}</td>'
               f'<td style="{TD}">{inr(t.get("gap")) if t.get("gap") is not None else "–"}</td>'
               f'<td style="{TD}">{spct(wow(t, r["ptot"]))}</td>'
               f'<td style="{TD}">{snap_cell(r)}</td>'
               f'<td style="{TD};white-space:normal">{ans}</td>'
               f'<td style="padding:5px 8px;border-bottom:1px solid #eaeef2;font-size:13px">'
               + "<br>".join(f"• {e(p)}" for p in r["press"]) + "</td></tr>")
    return (f'<h2 style="{CSS["h2"]}">Executive summary</h2>'
            + table(["Manager", "Week revenue", "vs target", "Gap", "vs prev week", "Month est. (Secondary)", "Answers",
                     "Press on"], tr))


def snap_cell(r):
    t, est = r.get("sec") or (None, None)
    if not t:
        return "–"
    warn = f' <span style="color:#9a6700">⚠ {r["snap_flags"]}</span>' if r.get("snap_flags") else ""
    return f"₹{est:,.1f} L of ₹{t:,.1f} L ({est / t * 100:.0f}%){warn}"


def render_manager(m, wk, pw, tot, ptot, qs, replies, summary, s):
    def f_roas(x):
        return f"{x['roas']:.2f}" if x.get("roas") else "–"

    def f_dvg(x):
        return "–" if x.get("disc_vs_guide") is None else f"{round(x['disc_vs_guide'], 1) + 0.0:+.1f} pp".replace("-", "−").replace("+0.0", "0.0")

    rows = ""
    for p in m["plats"]:
        v, vp = Week.metrics(wk.plat.get(p)), Week.metrics(pw.plat.get(p))
        if not v or (not v.get("rev") and not v.get("plan")):
            continue
        till = wk.till.get(p)
        rows += (f'<tr><td style="{TD}"><b>{e(p)}</b></td><td style="{TD}">{inr(v["rev"])}</td>'
                 f'<td style="{TD}">{inr(v["plan"]) if v["plan"] else "no plan"}</td>'
                 f'<td style="{TD}{_att_style(v.get("att"), s)}">{pct(v.get("att"))}</td>'
                 f'<td style="{TD}">{spct(wow(v, vp))}</td>'
                 f'<td style="{TD}">{inr(v["spend"]) if v["spend"] else "no ads"}</td>'
                 f'<td style="{TD}">{f_roas(v)} <span style="color:#59636e">({f_roas(vp)})</span></td>'
                 f'<td style="{TD}{_disc_style(v.get("disc_vs_guide"), s)}">{f_dvg(v)}</td>'
                 f'<td style="{TD};color:#59636e">{covered(wk, p)}</td></tr>')
    score = table(["Platform", "Week revenue", "Week target", "vs target", "vs prev week", "Ad spend", "ROAS (prev)",
                   "Disc vs guide", "Data covered"], rows)
    gaps = sorted([(k, Week.metrics(v)) for k, v in wk.cp.items() if k[1] in m["plats"] and v["plan"]],
                  key=lambda kv: kv[1]["gap"])[:5]
    grow = ""
    for (cat, p), v in gaps:
        if v["gap"] >= 0:
            continue
        vp = Week.metrics(pw.cp.get((cat, p)))
        grow += (f'<tr><td style="{TD}"><b>{e(cat)}</b> <span style="color:#59636e">· {e(p)}</span></td>'
                 f'<td style="{TD}">{inr(v["gap"])}</td><td style="{TD}{_att_style(v["att"], s)}">{pct(v["att"])}</td>'
                 f'<td style="{TD}">{spct(wow(v, vp))}</td>'
                 f'<td style="{TD}">{inr(v["spend"]) if v["spend"] else "no ads"}</td>'
                 f'<td style="{TD}{_disc_style(v.get("disc_vs_guide"), s)}">{f_dvg(v)}</td></tr>')
    gap_tbl = table(["Category · platform", "Gap", "vs target", "vs prev week", "Ad spend", "Disc vs guide"], grow) if grow else ""
    qa = ""
    for q in qs:
        qa += (f'<div style="{CSS["card"]}"><b style="color:#0b5394">{q["id"]}</b> {vbadge(q["verdict"])} '
               f'<span style="font-size:13px">{e(q["text"])}</span>')
        if q.get("answer"):
            qa += f'<div style="font-size:13px;margin-top:6px"><b>Answer:</b> {e(q["answer"])}</div>'
        if q.get("why"):
            qa += f'<div style="font-size:13px;margin-top:2px"><b>Why:</b> {e(q["why"])}</div>'
        if q.get("ask"):
            qa += f'<div style="font-size:13px;margin-top:2px;color:#0b5394"><b>Ask in WBR:</b> {e(q["ask"])}</div>'
        qa += "</div>"
    if qs and not replies:
        qa = f'<p style="color:#cf222e;font-size:13px"><b>No reply received</b> to the questions below.</p>' + qa
    if replies and not any(q.get("answer") for q in qs):  # Claude unavailable: show the raw reply
        qa += (f'<p style="font-size:13px"><b>Reply received:</b></p><pre style="white-space:pre-wrap;font-size:12px;'
               f'background:#f6f8fa;padding:8px;border-radius:6px">{e(replies[-1]["text"][:3000])}</pre>')
    if not qs:
        qa = f'<p style="{CSS["muted"]}">No questions were sent for this WBR.</p>'
    head = (f'<h2 style="{CSS["h2"]};margin-top:36px">{e(m["name"])} · {fmt_t(m["time"])}</h2>'
            f'<p style="{CSS["muted"]};margin:0 0 6px">{e(", ".join(m["plats"]))}'
            f'{" · CC " + e(", ".join(m["cc"])) if m["cc"] else ""}</p>'
            + (f'<p style="margin:6px 0 10px">{e(summary)}</p>' if summary else ""))
    return (head + f'<h3 style="{CSS["h3"]};margin-top:12px">Week scorecard</h3>' + score
            + (f'<h3 style="{CSS["h3"]}">Biggest gaps this week</h3>' + gap_tbl if gap_tbl else "")
            + f'<h3 style="{CSS["h3"]}">Questions & answers</h3>' + qa)


# ══════════════════════════════════════════════
#  Main
# ══════════════════════════════════════════════
def main():
    log.info(f"wbr.py – stage={STAGE or '?'} mode={MODE}{' force' if FORCE else ''}")
    if STAGE not in ("queries", "reminder", "pack"):
        log.info("  Nothing to do (stage must be queries, reminder or pack)")
        return
    cutoff = os.environ.get("WBR_NOT_AFTER", "").strip()
    if cutoff and NOW.strftime("%H:%M") > cutoff:
        log.warning(f"  Started at {NOW.strftime('%H:%M')} IST, after the {cutoff} cut-off for this backup run; nothing sent.")
        return
    month, d = load_current()
    all_plats = list(d["prorate_info"].keys())
    resolve_platforms(all_plats)
    managers = [m for m in wc.MANAGERS if not ONLY or m["key"] == ONLY]
    hist = snap.History(extra_current=snap.snapshot_from_payload(snap.load_processed(), TODAY))
    guide = {(r["Platform"], r["SKU"]): r["Guideline_Discount_Pct"] for r in d["discount_sku"]
             if r.get("Has_Guideline") and r.get("Guideline_Discount_Pct") is not None}
    names = {(r["Platform"], r["SKU"]): r.get("Short Name") or r["SKU"] for r in d["sku_detail"]}
    override = os.environ.get("WBR_DATE", "").strip()
    wbr_for = os.environ.get("WBR_FOR", "").strip().lower()
    wbr_target = dt.date.fromisoformat(override) if override else None
    if not wbr_target and wbr_for:  # "Monday WBRs" / "Tuesday WBRs" → the next such date (today counts)
        wd = 0 if wbr_for.startswith("mon") else 1
        wbr_target = TODAY + dt.timedelta(days=(wd - TODAY.weekday()) % 7)
    if wbr_target:
        log.info(f"  WBR date {wbr_target} · review period {fmt_range(*review_period(wbr_target))}")
    if wbr_target:
        managers = [m for m in managers if DAYS.index(m["day"]) == wbr_target.weekday()]
    try:
        with open("agent_state/brief_state.json") as f:
            brief_state = json.load(f)
    except Exception:
        brief_state = {}
    if STAGE == "queries":
        run_queries(wbr_target, managers, hist, guide, names, brief_state)
    elif STAGE == "reminder":
        run_reminders(wbr_target, managers)
    else:
        run_pack(wbr_target or TODAY, managers, hist, guide, names, d["summary"], d)
    log.info("✅ Done")


if __name__ == "__main__":
    main()
