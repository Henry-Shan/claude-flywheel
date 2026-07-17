#!/usr/bin/env python3
"""claude-flywheel · dashboard.

Answers "is the flywheel working, and is it making Claude better?" — health,
live injection feed, lesson catalog, mining/autopilot status, and the KPIs from
docs/METRICS.md (per-lesson before/after friction, coverage, trends).

Modes:
  python3 dashboard.py --serve [--port N] [--open]   # LIVE server (auto-refresh)
  python3 dashboard.py [--open]                       # write a static HTML file
  python3 dashboard.py --text                         # terminal summary
  python3 dashboard.py --health                        # health only (exit 0/1)

stdlib only. One renderer drives both live and static (JS reads window.__DATA__
when embedded, else polls /api/data).
"""

import argparse
import html
import json
import os
import re
import sys
import time

HOME = os.path.expanduser("~")
FLYWHEEL = os.path.join(HOME, ".claude", "flywheel")
GLOBAL_LESSONS = os.path.join(FLYWHEEL, "lessons")
STATE = os.path.join(FLYWHEEL, "state")
CONFIG = os.path.join(FLYWHEEL, "config.json")
OUT_HTML = os.path.join(FLYWHEEL, "dashboard.html")
PLUGIN_CACHE = os.path.join(HOME, ".claude", "plugins", "cache", "claude-flywheel")
DAY = 86400
MARKER = "<!--FLYWHEEL_DATA-->"   # static-embed injection point in the HTML shell


# ----------------------------------------------------------------- small utils
def read_jsonl(name):
    rows = []
    try:
        with open(os.path.join(STATE, name), encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):   # ignore bare scalars/arrays
                    rows.append(obj)
    except OSError:
        pass
    return rows


def to_int(v, d=0):
    try:
        return int(str(v).strip())
    except (ValueError, TypeError):
        return d


def median(xs):
    xs = sorted(x for x in xs if x is not None)
    n = len(xs)
    if not n:
        return None
    return xs[n // 2] if n % 2 else round((xs[n // 2 - 1] + xs[n // 2]) / 2, 1)


def ago(ts, now=None):
    now = now or time.time()
    if not ts:
        return "never"
    d = now - ts
    if d < 90:
        return "just now"
    if d < 3600:
        return f"{int(d//60)}m ago"
    if d < DAY:
        return f"{int(d//3600)}h ago"
    return f"{int(d//DAY)}d ago"


def when(ts):
    return time.strftime("%b %-d, %H:%M", time.localtime(ts)) if ts else "—"


def parse_frontmatter(text):
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    meta, last = {}, None
    for line in text[3:end].splitlines():
        if not line.strip():
            continue
        if line[:1] in (" ", "\t") and last:
            meta[last] = (meta.get(last, "") + " " + line.strip().strip("\"'")).strip()
            continue
        s = line.strip()
        if s.startswith("#") or ":" not in s:
            continue
        k, _, v = s.partition(":")
        meta[k.strip()] = re.split(r"\s+#", v.strip().strip("\"'"))[0].strip()
        last = k.strip()
    return meta


_STOP = frozenset("the a an and or to of in on at for with is are be this that it".split())


def tokset(text):
    return {w for w in re.findall(r"[a-z0-9][a-z0-9_-]{2,}", (text or "").lower())
            if len(w) >= 3 and w not in _STOP}


def find_project_root(cwd):
    path = os.path.abspath(cwd or os.getcwd())
    for _ in range(12):
        if os.path.isdir(os.path.join(path, ".claude", "lessons")):
            return path
        p = os.path.dirname(path)
        if p == path:
            break
        path = p
    return None


# --------------------------------------------------------------------- loaders
def load_lessons(project_root):
    tiers = [("global", GLOBAL_LESSONS)]
    if project_root:
        tiers.insert(0, ("project", os.path.join(project_root, ".claude", "lessons")))
    out, seen = [], set()
    for tier, d in tiers:
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if not name.endswith(".md") or name.startswith("."):
                continue
            path = os.path.join(d, name)
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    meta = parse_frontmatter(fh.read(32768))
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            lid = meta.get("id") or name[:-3]
            if lid in seen:
                continue
            seen.add(lid)
            out.append({
                "id": lid, "tier": tier, "mtime": mtime,
                # immutable activation stamp — split on this, NOT mtime (a lesson
                # edit bumps mtime and would silently march the before/after line)
                "created": meta.get("created", ""),
                "scope": meta.get("scope", "?"), "class": meta.get("class", "?"),
                "signal": meta.get("signal", "?"),
                "symptom": meta.get("symptom", ""),
                "occurrences": to_int(meta.get("occurrences"), 1),
                "helpful": to_int(meta.get("helpful")), "harmful": to_int(meta.get("harmful")),
                "keywords": meta.get("keywords", ""),
                "_terms": tokset(meta.get("keywords", "") + " " + meta.get("symptom", "")
                                 + " " + lid.replace("-", " ")),
            })
    return out


def autopilot_state():
    enabled, mode = False, "scoped"
    try:
        with open(CONFIG, encoding="utf-8") as fh:
            a = (json.load(fh) or {}).get("automation", {}) or {}
        enabled, mode = bool(a.get("enabled")), a.get("permissionMode", "scoped")
    except (OSError, ValueError):
        pass
    mt = lambda n: (os.path.getmtime(os.path.join(STATE, n)) if os.path.exists(os.path.join(STATE, n)) else 0)
    return {"enabled": enabled, "mode": mode,
            "last_mine": mt("last-automine"), "last_consolidate": mt("last-autoconsolidate")}


def health(lessons):
    checks = []
    inst = os.path.isdir(PLUGIN_CACHE)
    checks.append(["Plugin installed", inst,
                   "in plugin cache" if inst else "not installed — /plugin install flywheel@claude-flywheel"])
    hook_ok, det = False, "no cached inject.py"
    if inst:
        for root, _d, files in os.walk(PLUGIN_CACHE):
            if "inject.py" in files:
                try:
                    import py_compile
                    py_compile.compile(os.path.join(root, "inject.py"), doraise=True)
                    hook_ok, det = True, "hooks compile"
                except Exception as e:  # noqa: BLE001
                    det = f"inject.py won't compile: {e}"
                break
    checks.append(["Hooks runnable", hook_ok, det])
    checks.append(["python3", sys.version_info >= (3, 7), sys.version.split()[0]])
    checks.append(["Lessons loaded", len(lessons) > 0, f"{len(lessons)} across tiers"])
    ap = autopilot_state()
    checks.append(["Autopilot", True,
                   (f"on ({ap['mode']}) · last mine " + (ago(ap['last_mine']) if ap['last_mine'] else "not yet"))
                   if ap["enabled"] else "off — mining/consolidate are manual"])
    ok = all(c[1] for c in checks)
    return ok, checks


# ------------------------------------------------------------------------ KPIs
MATCH_MIN = 3       # distinct term overlap for a session to "hit" a lesson's problem
N_GATE = 5          # min sessions per cell before we'll show a verdict
MIN_EFFECT = 1.0    # min |difference-in-differences| to call an effect (else "no clear effect")


def iso_epoch(s):
    if not s:
        return 0
    s = str(s).strip()
    if s.isdigit():
        return int(s)
    try:
        import calendar
        return calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return 0


def kpis(lessons, metrics, injections, now):
    """The honest measurement (see docs/METRICS.md). Per lesson we run a
    DIFFERENCE-IN-DIFFERENCES on friction:

      Δ_matched  = median(friction | matched task, AFTER activation)
                 − median(friction | matched task, BEFORE activation, trigger excluded)
      Δ_baseline = same before/after gap on UNCOVERED sessions (the global trend)
      DiD        = Δ_matched − Δ_baseline      (negative ⇒ the lesson helped
                                                beyond whatever changed globally)

    This differences out model upgrades / operator learning (they move covered
    and uncovered alike), splits on an IMMUTABLE activation time (frontmatter
    `created`, else first injection) so lesson edits can't move the boundary,
    and drops the highest-friction pre session (the incident that spawned the
    lesson) to blunt regression-to-the-mean. It is still a quasi-experiment, not
    proof — N and the confounds are shown, and a verdict only renders when every
    cell clears N_GATE and |DiD| ≥ MIN_EFFECT."""
    real = [m for m in metrics
            if not m.get("resumed") and (m.get("human_turns") or 0) >= 1
            and (m.get("started") or 0) > 0]          # drop unparseable timestamps

    lesson_terms = [(L["id"], L["_terms"]) for L in lessons if L["_terms"]]

    def best_overlap(mterms):
        return max((len(mterms & lt) for _i, lt in lesson_terms), default=0)

    # first-injection time per lesson (fallback activation, immutable once seen)
    first_inj = {}
    for r in injections:
        lid, ts = r.get("lesson"), r.get("ts", 0)
        if lid and ts and (lid not in first_inj or ts < first_inj[lid]):
            first_inj[lid] = ts

    uncovered = [m for m in real if best_overlap(set(m.get("terms") or [])) < MATCH_MIN]

    def split(sessions, cut):
        pre = [m for m in sessions if m["started"] < cut]
        post = [m for m in sessions if m["started"] >= cut]
        return pre, post

    per_lesson = []
    for L in lessons:
        lt = L["_terms"]
        if not lt:
            continue
        matched = [m for m in real if len(set(m.get("terms") or []) & lt) >= MATCH_MIN]
        activation = iso_epoch(L.get("created")) or first_inj.get(L["id"], 0)
        row = {"id": L["id"], "n_matched": len(matched), "activated": activation,
               "status": "insufficient", "did": None, "confident": False,
               "matched_before": 0, "matched_after": 0}
        if activation:
            pre, post = split(matched, activation)
            # exclude the likely trigger: the worst-friction pre session
            if len(pre) >= 2:
                pre = sorted(pre, key=lambda m: m.get("friction", 0))[:-1]
            u_pre, u_post = split(uncovered, activation)
            row["matched_before"], row["matched_after"] = len(pre), len(post)
            if min(len(pre), len(post), len(u_pre), len(u_post)) >= N_GATE:
                d_m = median([m["friction"] for m in post]) - median([m["friction"] for m in pre])
                d_b = median([m["friction"] for m in u_post]) - median([m["friction"] for m in u_pre])
                did = round(d_m - d_b, 1)
                row.update({
                    "did": did, "delta_matched": round(d_m, 1), "delta_baseline": round(d_b, 1),
                    "friction_before": median([m["friction"] for m in pre]),
                    "friction_after": median([m["friction"] for m in post]),
                    "confident": abs(did) >= MIN_EFFECT,
                    "status": "measured",
                })
        per_lesson.append(row)
    per_lesson.sort(key=lambda x: (0 if x["confident"] else 1, -x["n_matched"]))

    covered = sum(1 for m in real if best_overlap(set(m.get("terms") or [])) >= MATCH_MIN)
    weeks = {}
    for m in real:
        wk = int((now - m["started"]) // (7 * DAY))
        if wk < 12:
            weeks.setdefault(wk, []).append(m.get("friction", 0))
    trend = [{"weeks_ago": w, "friction": median(v), "n": len(v)}
             for w, v in sorted(weeks.items(), reverse=True)]

    return {
        "sessions_measured": len(real),
        "median_friction": median([m.get("friction", 0) for m in real]),
        "median_rounds": median([m.get("human_turns", 0) for m in real]),
        "coverage_pct": round(100 * covered / len(real)) if real else 0,
        "verdicts": [p for p in per_lesson if p["confident"]],
        "per_lesson": per_lesson,
        "friction_trend": trend,
        "method": "difference-in-differences vs uncovered sessions; immutable activation; trigger excluded",
    }


# --------------------------------------------------------------------- collect
def collect(project_root):
    now = time.time()
    lessons = load_lessons(project_root)
    ok, checks = health(lessons)
    inj = read_jsonl("injections.jsonl")
    metrics = read_jsonl("session-metrics.jsonl")
    fired = {}
    for r in inj:
        fired.setdefault(r.get("lesson"), []).append(r.get("ts", 0))
    ap = autopilot_state()
    K = kpis(lessons, metrics, inj, now)

    lesson_view = []
    for L in sorted(lessons, key=lambda x: (-x["occurrences"], x["id"])):
        f = fired.get(L["id"], [])
        lesson_view.append({
            "id": L["id"], "tier": L["tier"], "scope": L["scope"], "class": L["class"],
            "signal": L["signal"], "symptom": L["symptom"][:120],
            "occurrences": L["occurrences"], "helpful": L["helpful"], "harmful": L["harmful"],
            "fired": len(f), "last_fired": (max(f) if f else 0),
        })

    return {
        "generated": now,
        "health": {"ok": ok, "checks": checks},
        "autopilot": ap,
        "totals": {
            "lessons": len(lessons),
            "by_scope": _count(lessons, "scope"),
            "injections": len(inj),
            "inj_7d": sum(1 for r in inj if now - r.get("ts", 0) <= 7 * DAY),
            "last_injection": max((r.get("ts", 0) for r in inj), default=0),
            "helpful": sum(L["helpful"] for L in lessons),
            "harmful": sum(L["harmful"] for L in lessons),
        },
        "kpis": K,
        "injections": sorted(inj, key=lambda r: -r.get("ts", 0))[:50],
        "lessons": lesson_view,
    }


def _count(items, key):
    out = {}
    for it in items:
        out[it.get(key, "?")] = out.get(it.get(key, "?"), 0) + 1
    return out


# ------------------------------------------------------------------------ text
def text_report(d):
    L = ["claude-flywheel — status", "=" * 42,
         f"health: {'OK' if d['health']['ok'] else 'PROBLEM'}"]
    for n, g, det in d["health"]["checks"]:
        L.append(f"  [{'x' if g else ' '}] {n}: {det}")
    t, k = d["totals"], d["kpis"]
    L += ["",
          f"lessons: {t['lessons']}  ({', '.join(f'{a} {b}' for a,b in t['by_scope'].items())})",
          f"injections: {t['injections']} ({t['inj_7d']} in 7d, last {ago(t['last_injection'])})",
          f"sessions measured: {k['sessions_measured']}  median friction {k['median_friction']}  "
          f"median rounds {k['median_rounds']}  coverage {k['coverage_pct']}%",
          ""]
    conf = k.get("verdicts", [])
    L.append("did it improve? (matched-task friction, differenced vs the global trend)")
    if conf:
        for p in conf[:8]:
            arrow = "↓ helped" if (p["did"] or 0) < 0 else "↑ hurt"
            L.append(f"  {p['id']:32} {p['friction_before']}→{p['friction_after']} "
                     f"(trend {p['delta_baseline']:+}) net {p['did']:+} "
                     f"(n {p['matched_before']}→{p['matched_after']}) {arrow}")
    else:
        partial = [p for p in k["per_lesson"] if p.get("n_matched", 0) > 0]
        L.append("  can't tell yet — no lesson has ≥5 matched sessions BOTH before and")
        L.append("  after its activation (plus a baseline cohort). Collecting data.")
        if partial:
            L.append(f"  {len(partial)} lesson(s) have matched some sessions so far.")
    return "\n".join(L)


# ------------------------------------------------------------------------ html
SHELL = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>flywheel</title><style>
:root{--bg:#0b0e14;--card:#141922;--card2:#0f141c;--line:#232c3a;--tx:#e6edf3;--dim:#8b98a6;--acc:#3fb950;--warn:#d29922;--bad:#f85149;--blue:#58a6ff;--mono:ui-monospace,SFMono-Regular,Menlo,monospace}
@media(prefers-color-scheme:light){:root{--bg:#f6f8fa;--card:#fff;--card2:#f9fbfd;--line:#e4e9ef;--tx:#1f2933;--dim:#69707a;--acc:#1a7f37}}
*{box-sizing:border-box}html,body{margin:0}body{background:var(--bg);color:var(--tx);font:14.5px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:30px 20px 70px}
.top{display:flex;align-items:baseline;justify-content:space-between;gap:12px;flex-wrap:wrap}
h1{font-size:22px;letter-spacing:-.02em;margin:0;display:flex;gap:9px;align-items:center}
.live{font-size:11px;color:var(--acc);display:flex;align-items:center;gap:6px}
.live .dot{width:7px;height:7px;border-radius:50%;background:var(--acc);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
.sub{color:var(--dim);margin:2px 0 22px;font-size:13px}
h2{font-size:12px;text-transform:uppercase;letter-spacing:.07em;color:var(--dim);margin:30px 0 12px;font-weight:600}
.pill{font-size:12px;font-weight:600;padding:3px 10px;border-radius:999px}
.ok{background:color-mix(in srgb,var(--acc) 16%,transparent);color:var(--acc)}
.bad{background:color-mix(in srgb,var(--bad) 16%,transparent);color:var(--bad)}
.grid{display:grid;gap:12px}.g6{grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:13px;padding:16px}
.stat .v{font-size:25px;font-weight:650;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.stat .l{font-size:12.5px;color:var(--dim);margin-top:2px}.stat .s{font-size:11.5px;color:var(--dim);margin-top:3px;opacity:.85}
.checks{display:grid;gap:7px}.chk{display:flex;align-items:center;gap:9px;font-size:13.5px}
.chk .d{width:8px;height:8px;border-radius:50%;flex:0 0 auto}.chk.y .d{background:var(--acc)}.chk.n .d{background:var(--bad)}.chk .det{color:var(--dim);font-size:12.5px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:var(--dim);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.04em;padding:7px 10px;border-bottom:1px solid var(--line)}
td{padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}tr:last-child td{border-bottom:0}
.mono{font-family:var(--mono);font-size:12px}.dim{color:var(--dim)}.terms{color:var(--blue);font-family:var(--mono);font-size:11.5px}
.num{text-align:right;font-variant-numeric:tabular-nums}.pos{color:var(--acc)}.neg{color:var(--bad)}
.sub2{color:var(--dim);font-size:11.5px;margin-top:2px}
.tag{font-size:10.5px;padding:2px 7px;border-radius:6px;background:var(--line);color:var(--dim)}
.tag.global{background:color-mix(in srgb,var(--blue) 18%,transparent);color:var(--blue)}
.tag.project{background:color-mix(in srgb,var(--acc) 18%,transparent);color:var(--acc)}
.delta{font-weight:650;font-variant-numeric:tabular-nums}.delta.good{color:var(--acc)}.delta.worse{color:var(--warn)}
.faint{opacity:.5}
.empty{text-align:center;padding:34px 20px;color:var(--dim)}.empty .b{font-size:16px;color:var(--tx);margin-bottom:5px}
.spark{display:flex;align-items:flex-end;gap:3px;height:34px}.spark i{flex:1;background:var(--blue);border-radius:2px 2px 0 0;min-height:2px;opacity:.8}
.wrapT{overflow-x:auto;border:1px solid var(--line);border-radius:13px}
.foot{color:var(--dim);font-size:11.5px;margin-top:36px;text-align:center}
a{color:var(--blue)}
</style></head><body><div class=wrap>
<div class=top><h1>🎯 flywheel <span id=hpill class=pill></span></h1><div class=live id=live></div></div>
<div class=sub id=sub></div>
<h2>Health</h2><div class="card checks" id=health></div>
<h2>At a glance</h2><div class="grid g6" id=stats></div>
<h2>Did it make Claude better? · net of the global trend (difference-in-differences)</h2><div class=wrapT id=improve></div>
<h2>Injection feed — is it firing?</h2><div class=wrapT id=timeline></div>
<h2>Lesson catalog</h2><div class=wrapT id=catalog></div>
<div class=foot>flywheel · state ~/.claude/flywheel/state · <span id=mode></span></div>
</div>
<!--FLYWHEEL_DATA-->
<script>
const E=(t,c,h)=>{const e=document.createElement(t);if(c)e.className=c;if(h!=null)e.innerHTML=h;return e};
const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[m]));
const ago=ts=>{if(!ts)return'never';const d=Date.now()/1000-ts;if(d<90)return'just now';if(d<3600)return Math.floor(d/60)+'m ago';if(d<86400)return Math.floor(d/3600)+'h ago';return Math.floor(d/86400)+'d ago'};
const when=ts=>ts?new Date(ts*1000).toLocaleString([], {month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}):'—';
function render(d){
  document.getElementById('hpill').className='pill '+(d.health.ok?'ok':'bad');
  document.getElementById('hpill').textContent=d.health.ok?'healthy':'check';
  document.getElementById('sub').textContent='Self-improving memory · updated '+when(d.generated);
  // health
  const H=document.getElementById('health');H.innerHTML='';
  d.health.checks.forEach(([n,g,det])=>{const r=E('div','chk '+(g?'y':'n'));r.append(E('span','d'),E('b',null,esc(n)),E('span','det',esc(det)));H.append(r)});
  // stats
  const t=d.totals,k=d.kpis;
  const S=document.getElementById('stats');S.innerHTML='';
  const stat=(v,l,s)=>{const c=E('div','card stat');c.append(E('div','v',esc(v)),E('div','l',esc(l)));if(s)c.append(E('div','s',s));return c};
  const scope=Object.entries(t.by_scope).map(([a,b])=>b+' '+a).join(' · ');
  S.append(stat(t.lessons,'lessons',scope),
    stat(t.injections,'injections',t.inj_7d+' in 7d'),
    stat(ago(t.last_injection),'last fired',''),
    stat(k.median_friction==null?'—':k.median_friction,'median friction','lower = smoother'),
    stat(k.median_rounds==null?'—':k.median_rounds,'median rounds',k.sessions_measured+' sessions'),
    stat(k.coverage_pct+'%','coverage','tasks a lesson knows'));
  // improve table — difference-in-differences (see docs/METRICS.md)
  const imp=document.getElementById('improve');
  const conf=(k.verdicts||[]), matched=k.per_lesson.filter(p=>(p.n_matched||0)>0);
  if(conf.length){
    imp.innerHTML='';const tb=E('table');
    tb.innerHTML='<thead><tr><th>lesson</th><th class=num>friction before→after</th><th class=num>baseline trend</th><th class=num>net effect (DiD)</th><th class=num>n b→a</th></tr></thead>';
    const body=E('tbody');
    conf.forEach(p=>{const better=(p.did||0)<0;const bt=(p.delta_baseline>0?'+':'')+p.delta_baseline;
      body.innerHTML+=`<tr><td><b>${esc(p.id)}</b><div class=sub2>vs concurrent uncovered-session trend</div></td>`
        +`<td class=num>${p.friction_before} → ${p.friction_after}</td>`
        +`<td class="num dim">${bt}</td>`
        +`<td class="num delta ${better?'good':'worse'}">${better?'▼':'▲'} ${Math.abs(p.did)}</td>`
        +`<td class="num dim">${p.matched_before}→${p.matched_after}</td></tr>`});
    tb.append(body);imp.append(tb);
  } else {
    imp.innerHTML=`<div class=empty><div class=b>Collecting data — can't tell yet</div><p>Per lesson, this asks: did tasks that hit its problem get less painful <b>after</b> it was activated — beyond the trend on unrelated tasks (a difference-in-differences, so a model upgrade or you getting faster isn't miscredited to a lesson). Needs ≥5 matched sessions before <b>and</b> after activation, plus a baseline cohort.${matched.length?` ${matched.length} lesson(s) have matched some sessions.`:''}<br>Backfill history: <span class=mono>python3 ~/claude-flywheel/scripts/metrics.py backfill --since 60d</span></p></div>`;
  }
  // timeline
  const tl=document.getElementById('timeline');
  if(d.injections.length){tl.innerHTML='';const tb=E('table');tb.innerHTML='<thead><tr><th>when</th><th>lesson</th><th>tier</th><th>matched terms</th></tr></thead>';const b=E('tbody');
    d.injections.forEach(r=>{b.innerHTML+=`<tr><td class=mono>${when(r.ts)}</td><td><b>${esc(r.lesson)}</b></td><td class=dim>${esc(r.tier||'')}</td><td class=terms>${esc((r.matched||[]).slice(0,6).join(', '))}</td></tr>`});
    tb.append(b);tl.append(tb);
  } else tl.innerHTML=`<div class=empty><div class=b>No injections yet</div><p>The hook records here when a prompt matches a lesson. Fresh installs are empty; it fills as you work. (Hooks activate the session after install.)</p></div>`;
  // catalog
  const cat=document.getElementById('catalog');cat.innerHTML='';const ct=E('table');
  ct.innerHTML='<thead><tr><th>lesson</th><th>scope</th><th>class</th><th>signal</th><th class=num>occ</th><th class=num>help</th><th class=num>harm</th><th class=num>fired</th></tr></thead>';
  const cb=E('tbody');
  d.lessons.forEach(L=>{cb.innerHTML+=`<tr><td><b>${esc(L.id)}</b><div class=sub2>${esc(L.symptom)}</div></td>`
    +`<td><span class="tag ${esc(L.scope)}">${esc(L.scope)}</span></td><td class=dim>${esc(L.class)}</td><td class=mono>${esc(L.signal)}</td>`
    +`<td class=num>${L.occurrences}</td><td class="num pos">${L.helpful}</td><td class="num ${L.harmful?'neg':'dim'}">${L.harmful}</td>`
    +`<td class="num dim">${L.fired?ago(L.last_fired):'—'}</td></tr>`});
  ct.append(cb);cat.append(ct);
}
async function tick(){try{const r=await fetch('/api/data',{cache:'no-store'});render(await r.json());document.getElementById('live').innerHTML='<span class=dot></span>live';document.getElementById('mode').textContent='live · refreshes every 5s';}catch(e){document.getElementById('live').textContent='';}}
if(window.__DATA__){render(window.__DATA__);document.getElementById('mode').textContent='static snapshot — run `python3 dashboard.py --serve` for live';}
else{tick();setInterval(tick,5000);}
</script></body></html>"""


def render(embedded=None):
    if embedded is None:
        return SHELL
    # Escape breakout sequences so lesson-controlled strings can't close the
    # <script> or inject markup in the static-embed path (XSS review finding).
    blob = (json.dumps(embedded)
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
            .replace(chr(0x2028), "\\u2028").replace(chr(0x2029), "\\u2029"))
    inject = "<script>window.__DATA__=" + blob + ";</script>\n"
    if MARKER not in SHELL:  # marker must exist, else fail loudly rather than silently
        raise RuntimeError("dashboard template marker missing — cannot embed data")
    return SHELL.replace(MARKER, inject + MARKER, 1)


# ----------------------------------------------------------------------- serve
def serve(project_root, port, open_):
    import http.server
    import socketserver
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            try:
                if self.path.startswith("/api/data"):
                    self._send(200, json.dumps(collect(project_root)).encode(), "application/json")
                elif self.path in ("/", "/index.html"):
                    self._send(200, render(None).encode(), "text/html; charset=utf-8")
                else:
                    self._send(404, b"not found", "text/plain")
            except Exception as e:  # noqa: BLE001 — never leave a dead socket
                try:
                    self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")
                except OSError:
                    pass

    class Server(socketserver.ThreadingTCPServer):
        # reclaim the port immediately after a restart (else a just-killed
        # server's TIME_WAIT forces a fall-through to 8788, 8789, … and the
        # user's :8787 bookmark breaks)
        allow_reuse_address = True
        daemon_threads = True

    httpd = None
    for p in [port] + list(range(8787, 8797)):
        try:
            httpd = Server(("127.0.0.1", p), H)
            break
        except OSError:
            httpd = None
    if not httpd:
        print("could not bind a port", file=sys.stderr)
        sys.exit(1)
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    print(f"flywheel dashboard — live at {url}  (Ctrl-C to stop)")
    if open_:
        threading.Timer(0.5, lambda: os.system(
            f'{"open" if sys.platform=="darwin" else "xdg-open"} "{url}" >/dev/null 2>&1 &')).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")


# ------------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--open", action="store_true", dest="open_")
    ap.add_argument("--text", action="store_true")
    ap.add_argument("--health", action="store_true")
    ap.add_argument("--project", default=os.getcwd())
    args = ap.parse_args()
    root = find_project_root(args.project)

    if args.health:
        ok, checks = health(load_lessons(root))
        for n, g, d in checks:
            print(f"[{'x' if g else ' '}] {n}: {d}")
        sys.exit(0 if ok else 1)
    if args.serve:
        serve(root, args.port, args.open_)
        return
    if args.text:
        print(text_report(collect(root)))
        return
    try:
        os.makedirs(FLYWHEEL, exist_ok=True)
        with open(OUT_HTML, "w", encoding="utf-8") as fh:
            fh.write(render(collect(root)))
    except OSError as e:
        print(f"could not write: {e}", file=sys.stderr)
        sys.exit(1)
    d = collect(root)
    print(f"dashboard: {OUT_HTML}")
    print(f"health {'OK' if d['health']['ok'] else 'PROBLEM'} · {d['totals']['lessons']} lessons · "
          f"{d['totals']['injections']} injections · {d['kpis']['sessions_measured']} sessions measured")
    if args.open_:
        os.system(f'{"open" if sys.platform=="darwin" else "xdg-open"} "{OUT_HTML}" >/dev/null 2>&1 &')


if __name__ == "__main__":
    main()
