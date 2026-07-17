#!/usr/bin/env python3
"""claude-flywheel · dashboard generator.

Reads the flywheel's state + lesson store and answers one question: "is this
thing actually working?" Emits a self-contained HTML dashboard (no external
assets) and/or a terminal summary.

Usage:
  python3 dashboard.py                 # write HTML + print a one-line summary
  python3 dashboard.py --text          # full terminal report, no HTML
  python3 dashboard.py --open          # also open the HTML in a browser (macOS/Linux)
  python3 dashboard.py --project PATH   # include that project's lesson tier
  python3 dashboard.py --health         # health check only (exit 0 ok / 1 problem)

stdlib only. Reads:
  ~/.claude/flywheel/lessons/*.md          (global tier)
  <project>/.claude/lessons/*.md           (project tier)
  ~/.claude/flywheel/state/{injections,events,sessions,pending-mine}.jsonl
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
OUT_HTML = os.path.join(FLYWHEEL, "dashboard.html")
PLUGIN_CACHE = os.path.join(HOME, ".claude", "plugins", "cache", "claude-flywheel")
DAY = 86400


# --------------------------------------------------------------------------- IO
def read_jsonl(name):
    path = os.path.join(STATE, name)
    rows = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return rows


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


def load_lessons(project_root):
    tiers = [("global", GLOBAL_LESSONS)]
    if project_root:
        tiers.insert(0, ("project", os.path.join(project_root, ".claude", "lessons")))
    lessons, seen = [], set()
    for tier, d in tiers:
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if not name.endswith(".md") or name.startswith("."):
                continue
            try:
                with open(os.path.join(d, name), encoding="utf-8", errors="replace") as fh:
                    meta = parse_frontmatter(fh.read(32768))
            except OSError:
                continue
            lid = meta.get("id") or name[:-3]
            if lid in seen:
                continue
            seen.add(lid)
            meta["_id"] = lid
            meta["_tier"] = tier
            lessons.append(meta)
    return lessons


def find_project_root(cwd):
    path = os.path.abspath(cwd or os.getcwd())
    for _ in range(12):
        if os.path.isdir(os.path.join(path, ".claude", "lessons")):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return None


def to_int(v, d=0):
    try:
        return int(str(v).strip())
    except (ValueError, TypeError):
        return d


# ---------------------------------------------------------------------- health
def health(lessons):
    checks = []
    installed = os.path.isdir(PLUGIN_CACHE)
    checks.append(("Plugin installed", installed,
                   "found in plugin cache" if installed else "not in ~/.claude/plugins/cache — run /plugin install flywheel@claude-flywheel"))
    hook_ok = False
    detail = "no cached inject.py found"
    if installed:
        for root, _dirs, files in os.walk(PLUGIN_CACHE):
            if "inject.py" in files:
                p = os.path.join(root, "inject.py")
                try:
                    import py_compile
                    py_compile.compile(p, doraise=True)
                    hook_ok, detail = True, "inject.py + outcome_log.py present and compile"
                except Exception as e:  # noqa: BLE001
                    detail = f"inject.py fails to compile: {e}"
                break
    checks.append(("Hooks runnable", hook_ok, detail))
    py_ok = sys.version_info >= (3, 7)
    checks.append(("python3", py_ok, f"{sys.version.split()[0]}"))
    checks.append(("Lessons loaded", len(lessons) > 0,
                   f"{len(lessons)} lesson(s) across tiers"))
    ok = all(c[1] for c in checks)
    return ok, checks


# --------------------------------------------------------------------- metrics
def compute(lessons):
    inj = read_jsonl("injections.jsonl")
    events = read_jsonl("events.jsonl")
    sessions = read_jsonl("sessions.jsonl")
    pending = read_jsonl("pending-mine.jsonl")
    now = time.time()

    fired = {}
    for r in inj:
        fired.setdefault(r.get("lesson"), []).append(r)
    last_inj = max((r.get("ts", 0) for r in inj), default=0)

    by_scope = {}
    by_signal = {}
    for m in lessons:
        by_scope[m.get("scope", "?")] = by_scope.get(m.get("scope", "?"), 0) + 1
        by_signal[m.get("signal", "?")] = by_signal.get(m.get("signal", "?"), 0) + 1

    inj_7 = sum(1 for r in inj if now - r.get("ts", 0) <= 7 * DAY)
    inj_30 = sum(1 for r in inj if now - r.get("ts", 0) <= 30 * DAY)
    helpful = sum(to_int(m.get("helpful")) for m in lessons)
    harmful = sum(to_int(m.get("harmful")) for m in lessons)
    recurring = [m for m in lessons if to_int(m.get("occurrences"), 1) > 1]
    dead = [m for m in lessons if m["_id"] not in fired]
    mined = sum(1 for r in pending if r.get("mined"))

    return {
        "inj": inj, "events": events, "sessions": sessions, "pending": pending,
        "fired": fired, "last_inj": last_inj, "by_scope": by_scope,
        "by_signal": by_signal, "inj_total": len(inj), "inj_7": inj_7,
        "inj_30": inj_30, "helpful": helpful, "harmful": harmful,
        "recurring": recurring, "dead": dead,
        "queued": len(pending), "mined": mined, "now": now,
    }


def ago(ts, now):
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
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "—"


# ------------------------------------------------------------------------ text
def text_report(lessons, m, ok, checks):
    L = []
    L.append("claude-flywheel — status")
    L.append("=" * 42)
    L.append(f"health: {'OK' if ok else 'PROBLEM'}")
    for name, good, det in checks:
        L.append(f"  [{'x' if good else ' '}] {name}: {det}")
    L.append("")
    L.append(f"lessons: {len(lessons)}   " +
             " ".join(f"{k}={v}" for k, v in m['by_scope'].items()))
    L.append(f"injections: {m['inj_total']} total  ({m['inj_7']} in 7d, {m['inj_30']} in 30d)  "
             f"last: {ago(m['last_inj'], m['now'])}")
    L.append(f"counters: helpful={m['helpful']} harmful={m['harmful']}")
    L.append(f"mining queue: {m['queued']} queued, {m['mined']} mined")
    L.append(f"recurring mistakes (occ>1): {len(m['recurring'])}   "
             f"never-fired lessons: {len(m['dead'])}")
    if m["inj"]:
        L.append("")
        L.append("recent injections:")
        for r in sorted(m["inj"], key=lambda x: -x.get("ts", 0))[:8]:
            L.append(f"  {when(r.get('ts'))}  {r.get('lesson','?'):32}  "
                     f"[{', '.join(r.get('matched', [])[:5])}]")
    else:
        L.append("")
        L.append("no injections recorded yet — the hooks record here as you work.")
        L.append("(a matching prompt fires a lesson; check back after some sessions.)")
    return "\n".join(L)


# ------------------------------------------------------------------------ html
def _bar(segments):
    total = sum(v for _, v in segments) or 1
    cells = "".join(
        f'<span class="seg seg-{i}" style="width:{100*v/total:.1f}%" '
        f'title="{html.escape(k)}: {v}"></span>'
        for i, (k, v) in enumerate(segments) if v
    )
    return f'<div class="bar">{cells}</div>'


def html_report(lessons, m, ok, checks):
    esc = html.escape
    badge = lambda good: '<span class="pill ok">healthy</span>' if good else '<span class="pill bad">check</span>'

    checks_html = "".join(
        f'<div class="check {"good" if g else "bad"}">'
        f'<span class="dot"></span><b>{esc(n)}</b><span class="det">{esc(d)}</span></div>'
        for n, g, d in checks
    )

    # injection timeline
    if m["inj"]:
        rows = "".join(
            f"<tr><td class='mono'>{when(r.get('ts'))}</td>"
            f"<td><b>{esc(str(r.get('lesson','?')))}</b></td>"
            f"<td>{esc(str(r.get('tier','')))}</td>"
            f"<td class='terms'>{esc(', '.join(r.get('matched', [])[:6]))}</td>"
            f"<td class='mono dim'>{esc(os.path.basename(str(r.get('project','')) or '—'))}</td></tr>"
            for r in sorted(m["inj"], key=lambda x: -x.get("ts", 0))[:40]
        )
        timeline = f"<table><thead><tr><th>when</th><th>lesson</th><th>tier</th><th>matched terms</th><th>project</th></tr></thead><tbody>{rows}</tbody></table>"
    else:
        timeline = (
            "<div class='empty'><div class='big'>No injections yet</div>"
            "<p>The hooks are installed and will record here as you work. When a prompt's "
            "wording matches a lesson's symptom/keywords, the strategy is injected before "
            "Claude answers — and that event lands in this timeline. Check back after a few "
            "sessions, or run a session that hits a known problem.</p></div>"
        )

    # lessons catalog
    lrows = ""
    for L in sorted(lessons, key=lambda x: (-to_int(x.get("occurrences"), 1), x["_id"])):
        fired = m["fired"].get(L["_id"], [])
        last = max((r.get("ts", 0) for r in fired), default=0)
        harm = to_int(L.get("harmful"))
        lrows += (
            f"<tr><td><b>{esc(L['_id'])}</b><div class='sub'>{esc(L.get('symptom',''))[:110]}</div></td>"
            f"<td><span class='tag t-{esc(L.get('scope','')).replace(' ','')}'>{esc(L.get('scope','?'))}</span></td>"
            f"<td>{esc(L.get('class','?'))}</td>"
            f"<td class='mono'>{esc(L.get('signal','?'))}</td>"
            f"<td class='num'>{esc(str(L.get('occurrences','1')))}</td>"
            f"<td class='num pos'>{esc(str(L.get('helpful','0')))}</td>"
            f"<td class='num {'neg' if harm else ''}'>{harm}</td>"
            f"<td class='mono dim'>{ago(last, m['now']) if last else '—'}</td></tr>"
        )
    catalog = f"<table><thead><tr><th>lesson</th><th>scope</th><th>class</th><th>signal</th><th>occ</th><th>help</th><th>harm</th><th>last fired</th></tr></thead><tbody>{lrows}</tbody></table>"

    stat = lambda label, val, sub="": (
        f"<div class='stat'><div class='v'>{esc(str(val))}</div>"
        f"<div class='l'>{esc(label)}</div>{'<div class=s>'+esc(sub)+'</div>' if sub else ''}</div>"
    )
    stats = (
        stat("lessons", len(lessons), " · ".join(f"{k} {v}" for k, v in m["by_scope"].items()))
        + stat("injections", m["inj_total"], f"{m['inj_7']} in 7d")
        + stat("last fired", ago(m["last_inj"], m["now"]))
        + stat("helpful / harmful", f"{m['helpful']} / {m['harmful']}")
        + stat("recurring", len(m["recurring"]), "occ &gt; 1")
        + stat("mining queue", f"{m['mined']}/{m['queued']}", "mined/queued")
    )

    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>claude-flywheel · status</title>
<style>
:root{{--bg:#0d1117;--card:#161b22;--line:#26303c;--tx:#e6edf3;--dim:#8b98a6;--acc:#3fb950;--warn:#d29922;--bad:#f85149;--blue:#58a6ff;--mono:ui-monospace,SFMono-Regular,Menlo,monospace}}
@media(prefers-color-scheme:light){{:root{{--bg:#f6f8fa;--card:#fff;--line:#e2e8f0;--tx:#1f2933;--dim:#6b7684;--acc:#1a7f37}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--tx);font:15px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}}
.wrap{{max-width:1060px;margin:0 auto;padding:32px 20px 64px}}
h1{{font-size:24px;letter-spacing:-.02em;margin:0 0 2px;display:flex;align-items:center;gap:10px}}
h2{{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim);margin:34px 0 12px;font-weight:600}}
.subtitle{{color:var(--dim);margin:0 0 22px}}
.pill{{font-size:12px;font-weight:600;padding:3px 10px;border-radius:999px}}
.pill.ok{{background:color-mix(in srgb,var(--acc) 18%,transparent);color:var(--acc)}}
.pill.bad{{background:color-mix(in srgb,var(--bad) 18%,transparent);color:var(--bad)}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px}}
.checks{{display:grid;gap:8px}}
.check{{display:flex;align-items:center;gap:10px;font-size:14px}}.check .det{{color:var(--dim);font-size:13px}}
.check .dot{{width:9px;height:9px;border-radius:50%;flex:0 0 auto}}
.check.good .dot{{background:var(--acc)}}.check.bad .dot{{background:var(--bad)}}
.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}}
.stat{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}}
.stat .v{{font-size:26px;font-weight:650;letter-spacing:-.02em;font-variant-numeric:tabular-nums}}
.stat .l{{font-size:13px;color:var(--dim);margin-top:3px}}.stat .s{{font-size:12px;color:var(--dim);margin-top:4px;opacity:.8}}
table{{width:100%;border-collapse:collapse;font-size:13.5px}}
th{{text-align:left;color:var(--dim);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.04em;padding:8px 10px;border-bottom:1px solid var(--line)}}
td{{padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:top}}
tr:last-child td{{border-bottom:0}}
.mono{{font-family:var(--mono);font-size:12.5px}}.dim{{color:var(--dim)}}.terms{{color:var(--blue);font-family:var(--mono);font-size:12px}}
.num{{text-align:center;font-variant-numeric:tabular-nums}}.pos{{color:var(--acc)}}.neg{{color:var(--bad)}}
.sub{{color:var(--dim);font-size:12px;margin-top:2px}}
.tag{{font-size:11px;padding:2px 8px;border-radius:6px;background:var(--line);color:var(--dim)}}
.tag.t-global{{background:color-mix(in srgb,var(--blue) 20%,transparent);color:var(--blue)}}
.tag.t-project{{background:color-mix(in srgb,var(--acc) 20%,transparent);color:var(--acc)}}
.empty{{text-align:center;padding:40px 24px;color:var(--dim)}}.empty .big{{font-size:18px;color:var(--tx);margin-bottom:6px}}.empty p{{max-width:560px;margin:0 auto}}
.foot{{color:var(--dim);font-size:12px;margin-top:40px;text-align:center}}
.tbl-wrap{{overflow-x:auto;border:1px solid var(--line);border-radius:12px}}
</style></head><body><div class=wrap>
<h1>🎯 claude-flywheel {badge(ok)}</h1>
<p class=subtitle>Self-improving memory · generated {when(m['now'])}</p>

<h2>Health</h2>
<div class="card checks">{checks_html}</div>

<h2>At a glance</h2>
<div class=stats>{stats}</div>

<h2>Injection timeline — is it firing?</h2>
<div class=tbl-wrap>{timeline}</div>

<h2>Lesson catalog</h2>
<div class=tbl-wrap>{catalog}</div>

<p class=foot>Data: ~/.claude/flywheel/state/ · lessons: project .claude/lessons/ + ~/.claude/flywheel/lessons/ · regenerate with /flywheel:status</p>
</div></body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", action="store_true")
    ap.add_argument("--open", action="store_true", dest="open_")
    ap.add_argument("--health", action="store_true")
    ap.add_argument("--project", default=os.getcwd())
    args = ap.parse_args()

    project_root = find_project_root(args.project)
    lessons = load_lessons(project_root)
    ok, checks = health(lessons)

    if args.health:
        for n, g, d in checks:
            print(f"[{'x' if g else ' '}] {n}: {d}")
        sys.exit(0 if ok else 1)

    m = compute(lessons)

    if args.text:
        print(text_report(lessons, m, ok, checks))
        return

    try:
        os.makedirs(FLYWHEEL, exist_ok=True)
        with open(OUT_HTML, "w", encoding="utf-8") as fh:
            fh.write(html_report(lessons, m, ok, checks))
    except OSError as e:
        print(f"could not write dashboard: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"dashboard: {OUT_HTML}")
    print(f"health: {'OK' if ok else 'PROBLEM'} · {len(lessons)} lessons · "
          f"{m['inj_total']} injections (last {ago(m['last_inj'], m['now'])})")

    if args.open_:
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        os.system(f'{opener} "{OUT_HTML}" >/dev/null 2>&1 &')


if __name__ == "__main__":
    main()
