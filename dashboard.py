"""Static HTML dashboard generator.

Single self-contained file at plan_output/dashboard.html. No JS framework, no
external JS. The only external reference is the Google Fonts <link> for the
display + mono typefaces (with system fallbacks). Charts are pure CSS or inline
SVG. Open in any browser.

Design: an editorial "International Klein Blue / cobalt" poster. Warm off-white
ground, full-height cobalt countdown panel, oversized display numerals, mono
eyebrow labels with section index numbers (01..07), hairline rules, a cobalt
monochrome TSS heatmap, CSS bar charts, and inline-SVG line chart + probability
ring. Every colour + font is driven by config.theme().

Sections, in order:
  TOPBAR
  01/02  Fitness + Race prediction        (masthead above)
  01     Fitness / Training load          (line chart)
  02     Race prediction                  (VDOT + rows + verdict)
  03     Consistency                      (4-cell grid)
  04     Weekly mileage                   (12 CSS bars)
  05     Best efforts                     (table)
  06     Plan progress                    (phase bars)
  07     Training load heatmap            (13-week cobalt grid)
  FOOTER

Usage:
    python3 dashboard.py            # writes plan_output/dashboard.html
    python3 dashboard.py --open     # also opens in default browser
"""

import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import config


OUT_DIR = Path(__file__).parent / "plan_output"
OUT_FILE = OUT_DIR / "dashboard.html"

# r=46 circle circumference, used for the probability ring stroke-dasharray.
RING_CIRCUMFERENCE = 289.03


# ---------------------------------------------------------------------------
# Small formatting helpers
# ---------------------------------------------------------------------------

def _fmt_mmss(total_seconds: float) -> str:
    """Seconds -> 'M:SS' (pace style)."""
    total = int(round(total_seconds))
    m, s = divmod(total, 60)
    return f"{m}:{s:02d}"


def _pace_from_min(min_per_mi: float) -> str:
    """Decimal minutes-per-mile -> 'M:SS'."""
    return _fmt_mmss(min_per_mi * 60)


def _short_date(iso_or_date) -> str:
    """ISO date string (or date) -> 'AUG 11' style label."""
    if not iso_or_date:
        return "--"
    try:
        if isinstance(iso_or_date, date):
            d = iso_or_date
        else:
            d = date.fromisoformat(str(iso_or_date)[:10])
    except (ValueError, TypeError):
        return "--"
    return f"{d.strftime('%b').upper()} {d.day:02d}"


def _signed(n: int, unit: str = "") -> str:
    """Integer -> '+95s' / '-12s' style."""
    return f"{'+' if n >= 0 else '-'}{abs(n)}{unit}"


def _theme_vars(t: dict) -> str:
    """Build the :root CSS block from config.theme()."""
    heat = t["heatmap"]
    return (
        ":root{\n"
        f"  --klein:{t['klein']}; --klein-br:{t['klein_bright']};\n"
        f"  --paper:{t['paper']}; --paper-lt:{t['paper_light']}; --paper-dk:{t['paper_dark']};\n"
        f"  --ink:{t['ink']}; --verm:{t['vermilion']}; --white:{t['white']};\n"
        f"  --c1:{heat[0]}; --c2:{heat[1]}; --c3:{heat[2]}; --c4:{heat[3]}; --c5:{heat[4]};\n"
        f"  --tint:{t['tint']};\n"
        "  --rule:var(--ink); --rule-lt:rgba(10,10,10,.16);\n"
        "}"
    )


# ---------------------------------------------------------------------------
# CSS  (ported from the approved reference; colours/fonts via :root)
# ---------------------------------------------------------------------------

def _build_css(t: dict) -> str:
    disp = t["display_font"]
    mono = t["mono_font"]
    root = _theme_vars(t)
    return f"""<style>
*{{margin:0;padding:0;box-sizing:border-box}}
{root}
html{{-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}}
body{{
  background:var(--paper-dk); color:var(--ink);
  font-family:'{disp}',system-ui,sans-serif;
  padding:48px 24px; min-height:100vh;
}}
body::before{{
  content:""; position:fixed; inset:0; pointer-events:none; z-index:9999; opacity:.04;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='160' height='160'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E");
}}
.mono{{font-family:'{mono}',ui-monospace,monospace}}

.poster{{max-width:1200px; margin:0 auto; background:var(--paper-lt); border:1px solid var(--ink)}}
.band{{border-bottom:1px solid var(--ink)}}
.pad{{padding:42px 56px}}

.eyebrow{{display:flex; align-items:baseline; gap:18px; margin-bottom:26px}}
.eyebrow .idx{{font-family:'{mono}',monospace; font-size:13px; font-weight:700; color:var(--klein); letter-spacing:.04em}}
.eyebrow .lbl{{font-family:'{mono}',monospace; font-size:11.5px; font-weight:700; text-transform:uppercase; letter-spacing:.22em; color:var(--ink)}}
.eyebrow .rt{{margin-left:auto; font-family:'{mono}',monospace; font-size:11px; letter-spacing:.18em; color:rgba(10,10,10,.5); text-transform:uppercase}}

.topbar{{display:flex; align-items:center; justify-content:space-between;
  padding:13px 56px; font-family:'{mono}',monospace; font-size:11px;
  letter-spacing:.24em; text-transform:uppercase}}
.topbar .l{{font-weight:700}}
.topbar .c{{color:rgba(10,10,10,.55); letter-spacing:.3em}}
.topbar .r{{color:rgba(10,10,10,.55)}}

.mast{{display:grid; grid-template-columns:repeat(12,1fr)}}
.mast-main{{grid-column:1/9; padding:46px 56px 40px}}
.mast-eye{{display:flex; align-items:center; gap:14px; font-family:'{mono}',monospace;
  font-size:11.5px; letter-spacing:.24em; text-transform:uppercase; font-weight:700; margin-bottom:22px}}
.mast-eye .dot{{width:9px;height:9px;background:var(--verm);border-radius:50%}}
.mast-eye .sub{{color:rgba(10,10,10,.5); font-weight:400; letter-spacing:.18em}}
.title{{font-size:104px; line-height:.9; font-weight:700; letter-spacing:-.04em; margin:6px 0 0}}
.title .t1{{display:inline-block; font-size:60px; line-height:1; letter-spacing:-.02em}}
.title .b2{{color:var(--klein)}}
.athlete{{display:flex; align-items:baseline; gap:14px; margin-top:30px;
  font-family:'{mono}',monospace; text-transform:uppercase}}
.athlete .nm{{font-size:15px; font-weight:700; letter-spacing:.18em}}
.athlete .meta{{font-size:11.5px; letter-spacing:.16em; color:rgba(10,10,10,.55)}}

.mast-stats{{display:flex; align-items:flex-end; gap:0; margin-top:40px; border-top:1px solid var(--rule-lt); padding-top:26px}}
.kpi{{padding-right:22px; margin-right:22px; border-right:1px solid var(--rule-lt)}}
.kpi:last-child{{border-right:0;margin-right:0;padding-right:0}}
.kpi .k-lbl{{font-family:'{mono}',monospace; font-size:10px; letter-spacing:.2em; text-transform:uppercase; color:rgba(10,10,10,.55); margin-bottom:9px}}
.kpi .k-val{{font-size:30px; font-weight:700; letter-spacing:-.025em; line-height:1; white-space:nowrap}}
.kpi .k-val small{{font-size:13px; font-weight:500; color:rgba(10,10,10,.55); letter-spacing:0; margin-left:3px}}
.ring-wrap{{display:flex; align-items:center; gap:12px}}
.ring{{width:64px;height:64px;flex:none}}
.ring .rtxt{{font-family:'{disp}'; font-weight:700; font-size:24px; fill:var(--ink)}}
.ring-cap{{font-family:'{mono}',monospace; font-size:9px; letter-spacing:.14em; text-transform:uppercase; color:rgba(10,10,10,.55); line-height:1.5; max-width:72px}}

.mast-count{{grid-column:9/13; background:var(--klein); color:#fff; padding:44px 48px 40px;
  display:flex; flex-direction:column; justify-content:space-between}}
.mast-count .c-eye{{font-family:'{mono}',monospace; font-size:11px; letter-spacing:.26em; text-transform:uppercase; color:var(--tint)}}
.count-big{{display:flex; align-items:flex-start; gap:10px; margin-top:14px}}
.count-big .num{{font-size:140px; line-height:.82; font-weight:700; letter-spacing:-.05em}}
.count-big .days{{font-family:'{mono}',monospace; font-size:13px; letter-spacing:.22em; text-transform:uppercase; color:var(--tint); margin-top:14px}}
.race-day{{margin-top:30px; border-top:1px solid rgba(255,255,255,.28); padding-top:18px; display:flex; justify-content:space-between; align-items:baseline}}
.race-day .rd-l{{font-family:'{mono}',monospace; font-size:10.5px; letter-spacing:.2em; text-transform:uppercase; color:var(--tint)}}
.race-day .rd-v{{font-size:30px; font-weight:700; letter-spacing:-.02em}}
.race-day .rd-v small{{font-family:'{mono}',monospace; font-size:11px; font-weight:400; color:var(--tint); letter-spacing:.16em; margin-left:8px}}

.split{{display:grid}}
.split.s73{{grid-template-columns:1.72fr 1fr}}
.split.s37{{grid-template-columns:1fr 1.72fr}}
.split.s55{{grid-template-columns:1fr 1fr}}
.split > .col{{padding:42px 56px}}
.split > .col + .col{{border-left:1px solid var(--ink)}}

.fit-grid{{display:flex; gap:0; margin-bottom:30px}}
.fit-stat{{padding-right:40px; margin-right:40px; border-right:1px solid var(--rule-lt)}}
.fit-stat:last-child{{border:0;margin:0;padding:0}}
.fit-stat .fs-lbl{{font-family:'{mono}',monospace; font-size:10.5px; letter-spacing:.18em; text-transform:uppercase; color:rgba(10,10,10,.55); margin-bottom:12px}}
.fit-stat .fs-lbl b{{color:var(--ink)}}
.fit-stat .fs-val{{font-size:78px; line-height:.86; font-weight:700; letter-spacing:-.03em}}
.fit-stat .fs-val.neg{{color:var(--verm)}}
.fit-stat .fs-sub{{font-family:'{mono}',monospace; font-size:11px; letter-spacing:.06em; color:rgba(10,10,10,.55); margin-top:10px}}
.chart-head{{display:flex; justify-content:space-between; align-items:baseline; margin-bottom:10px}}
.chart-head .ph{{font-family:'{mono}',monospace; font-size:11px; letter-spacing:.16em; text-transform:uppercase}}
.chart-head .ph b{{color:var(--klein)}}
.legend-inline{{display:flex; gap:18px; font-family:'{mono}',monospace; font-size:10px; letter-spacing:.12em; text-transform:uppercase; color:rgba(10,10,10,.6)}}
.legend-inline span{{display:flex;align-items:center;gap:7px}}
.legend-inline i{{width:16px;height:3px;display:inline-block}}
.line-chart{{width:100%; height:auto; display:block}}

.pred-vdot{{font-size:78px; font-weight:700; letter-spacing:-.03em; line-height:.9}}
.pred-vdot small{{font-size:16px; font-weight:500; color:rgba(10,10,10,.55); letter-spacing:.04em}}
.pred-row{{display:flex; justify-content:space-between; align-items:baseline; padding:15px 0; border-top:1px solid var(--rule-lt)}}
.pred-row .pr-l{{font-family:'{mono}',monospace; font-size:11px; letter-spacing:.16em; text-transform:uppercase; color:rgba(10,10,10,.6)}}
.pred-row .pr-v{{font-size:22px; font-weight:700; letter-spacing:-.01em}}
.pred-row .pr-v.blue{{color:var(--klein)}}
.verdict{{margin-top:24px; padding-top:22px; border-top:1px solid var(--ink); font-size:19px; line-height:1.4; font-weight:500; letter-spacing:-.01em}}
.verdict .vk{{font-family:'{mono}',monospace; font-size:10px; letter-spacing:.22em; text-transform:uppercase; color:var(--klein); display:block; margin-bottom:12px; font-weight:700}}

.heat-wrap{{display:flex; flex-direction:column}}
.heat-months{{display:grid; grid-template-columns:repeat(13,1fr); margin:0 0 8px 26px; font-family:'{mono}',monospace; font-size:10px; letter-spacing:.14em; color:rgba(10,10,10,.5)}}
.heat-body{{display:flex; gap:8px}}
.heat-days{{display:grid; grid-template-rows:repeat(7,1fr); gap:5px; font-family:'{mono}',monospace; font-size:9px; letter-spacing:.1em; color:rgba(10,10,10,.4); width:18px}}
.heat-days span{{display:flex;align-items:center;height:100%}}
.heat-grid{{display:grid; grid-template-rows:repeat(7,1fr); grid-auto-flow:column; gap:5px; flex:1}}
.hc{{aspect-ratio:1; border-radius:1px}}
.hc.l0{{background:transparent; box-shadow:inset 0 0 0 1px rgba(10,10,10,.10)}}
.hc.l1{{background:var(--c1)}} .hc.l2{{background:var(--c2)}} .hc.l3{{background:var(--c3)}}
.hc.l4{{background:var(--c4)}} .hc.l5{{background:var(--c5)}}
.heat-foot{{display:flex; align-items:center; justify-content:space-between; margin-top:22px; padding-top:20px; border-top:1px solid var(--rule-lt)}}
.heat-foot .note{{font-family:'{mono}',monospace; font-size:11px; letter-spacing:.06em; color:rgba(10,10,10,.6)}}
.heat-foot .note b{{color:var(--ink)}}
.heat-legend{{display:flex; align-items:center; gap:8px; font-family:'{mono}',monospace; font-size:10px; letter-spacing:.16em; text-transform:uppercase; color:rgba(10,10,10,.55)}}
.heat-legend .sw{{width:15px;height:15px;border-radius:1px}}

.mile-chart{{display:flex; align-items:flex-end; gap:10px; height:230px; margin-top:8px}}
.bar-col{{flex:1; display:flex; flex-direction:column; align-items:center; height:100%; justify-content:flex-end}}
.bar-val{{font-family:'{mono}',monospace; font-size:11px; font-weight:700; margin-bottom:7px}}
.bar{{width:100%; max-width:34px; display:flex; flex-direction:column; justify-content:flex-end}}
.seg.easy{{background:var(--c3)}}
.seg.long{{background:var(--klein)}}
.bar-wk{{font-family:'{mono}',monospace; font-size:9.5px; color:rgba(10,10,10,.45); margin-top:9px; letter-spacing:.08em}}
.mile-foot{{display:flex; gap:24px; margin-top:22px; padding-top:18px; border-top:1px solid var(--rule-lt); font-family:'{mono}',monospace; font-size:10.5px; letter-spacing:.1em; text-transform:uppercase; color:rgba(10,10,10,.6)}}
.mile-foot span{{display:flex;align-items:center;gap:8px}}
.mile-foot i{{width:13px;height:13px;display:inline-block;border-radius:1px}}

.cons-grid{{display:grid; grid-template-columns:1fr 1fr; gap:0}}
.cons-cell{{padding:24px 22px; border-bottom:1px solid var(--rule-lt)}}
.cons-cell:nth-child(odd){{border-right:1px solid var(--rule-lt)}}
.cons-cell:nth-child(1),.cons-cell:nth-child(2){{padding-top:4px}}
.cons-cell:nth-child(3),.cons-cell:nth-child(4){{border-bottom:0}}
.cons-cell .c-val{{font-size:52px; font-weight:700; letter-spacing:-.03em; line-height:.9}}
.cons-cell .c-val small{{font-size:16px; font-weight:500; color:rgba(10,10,10,.55)}}
.cons-cell .c-lbl{{font-family:'{mono}',monospace; font-size:10px; letter-spacing:.16em; text-transform:uppercase; color:rgba(10,10,10,.55); margin-top:12px; line-height:1.5}}

.eff-table{{width:100%; border-collapse:collapse}}
.eff-table td{{padding:17px 0; border-top:1px solid var(--rule-lt); vertical-align:baseline}}
.eff-table tr:first-child td{{border-top:0}}
.ev-idx{{font-family:'{mono}',monospace; font-size:11px; color:rgba(10,10,10,.4); width:38px; font-weight:700}}
.ev-dist{{font-family:'{mono}',monospace; font-size:13px; font-weight:700; letter-spacing:.12em; width:78px}}
.ev-time{{font-size:30px; font-weight:700; letter-spacing:-.02em}}
.ev-time.muted{{font-size:30px; color:rgba(10,10,10,.25)}}
.ev-pace{{font-family:'{mono}',monospace; font-size:12px; color:rgba(10,10,10,.55); text-align:right; letter-spacing:.04em}}
.ev-date{{font-family:'{mono}',monospace; font-size:11px; color:rgba(10,10,10,.45); text-align:right; width:74px; letter-spacing:.08em}}

.plan-top{{display:flex; justify-content:space-between; align-items:baseline; margin-bottom:30px}}
.plan-top .pt-wk{{font-size:30px; font-weight:700; letter-spacing:-.02em; white-space:nowrap}}
.plan-top .pt-wk small{{font-family:'{mono}',monospace; font-size:12px; font-weight:400; color:rgba(10,10,10,.55); letter-spacing:.06em}}
.plan-top .pt-mi{{font-family:'{mono}',monospace; font-size:11.5px; line-height:1.7; letter-spacing:.08em; text-transform:uppercase; text-align:right; color:rgba(10,10,10,.55); white-space:nowrap}}
.plan-top .pt-mi b{{color:var(--ink); font-size:13px}}
.phases{{display:grid; gap:6px}}
.phase .ph-bar{{height:10px; background:var(--c1)}}
.phase.done .ph-bar{{background:var(--c3)}}
.phase.cur .ph-bar{{background:var(--klein); position:relative}}
.phase.cur .ph-bar::after{{content:"";position:absolute;left:36%;top:-4px;bottom:-4px;width:2px;background:var(--verm)}}
.phase .ph-lbl{{font-family:'{mono}',monospace; font-size:11px; letter-spacing:.14em; text-transform:uppercase; margin-top:12px; font-weight:700}}
.phase .ph-wk{{font-family:'{mono}',monospace; font-size:9.5px; letter-spacing:.1em; color:rgba(10,10,10,.45); margin-top:5px}}
.phase.cur .ph-lbl{{color:var(--klein)}}
.phase:not(.cur):not(.done) .ph-lbl{{color:rgba(10,10,10,.4)}}
.wk-prog{{margin-top:32px; padding-top:24px; border-top:1px solid var(--rule-lt)}}
.wk-prog .wp-head{{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:12px}}
.wk-prog .wp-l{{font-family:'{mono}',monospace; font-size:10.5px; letter-spacing:.16em; text-transform:uppercase; color:rgba(10,10,10,.6)}}
.wk-prog .wp-v{{font-family:'{mono}',monospace; font-size:12px; font-weight:700}}
.wk-prog .wp-track{{height:14px; background:var(--c1); position:relative}}
.wk-prog .wp-fill{{height:100%; background:var(--klein)}}

.foot{{display:flex; justify-content:space-between; align-items:center; padding:20px 56px;
  font-family:'{mono}',monospace; font-size:10.5px; letter-spacing:.18em; text-transform:uppercase; color:rgba(10,10,10,.5)}}
.foot .fl b{{color:var(--ink)}}

@media (max-width:760px){{
  body{{padding:16px 8px}}
  .pad,.split>.col,.mast-main,.mast-count{{padding:28px 24px}}
  .title{{font-size:64px}}
  .title .t1{{font-size:40px}}
  .count-big .num{{font-size:96px}}
  .mast,.split.s73,.split.s37,.split.s55{{grid-template-columns:1fr}}
  .mast-main{{grid-column:auto}}.mast-count{{grid-column:auto}}
  .split>.col+.col{{border-left:0;border-top:1px solid var(--ink)}}
  .fit-stat .fs-val{{font-size:54px}}
}}
</style>"""


# ---------------------------------------------------------------------------
# Section builders  (emit the reference design's blocks with live values)
# ---------------------------------------------------------------------------

def _topbar() -> str:
    return """  <!-- TOPBAR -->
  <div class="topbar band">
    <div class="l">strava-run-coach</div>
    <div class="c">Training Report</div>
  </div>"""


def _masthead(race: dict) -> str:
    name = race["race_name"]
    distance = race.get("distance_mi", 0)
    goal_time = race.get("goal_time", "")
    goal_pace = _pace_from_min(race.get("goal_pace_min_per_mi", 0))
    pred = race.get("predicted_time", "N/A")
    pct = race.get("probability_pct", 0)
    days = race.get("days_to_race", 0)
    race_day = race.get("race_day_str", "")
    weekday = race.get("race_weekday", "")
    athlete = race.get("athlete_name", "Athlete").upper()

    # Title: all-but-last word small on line 1, last word large cobalt on line 2.
    words = name.split()
    if len(words) <= 1:
        title_html = f'<span class="b2">{name}</span>'
    else:
        line1 = " ".join(words[:-1])
        last = words[-1]
        title_html = f'<span class="t1">{line1}</span><br><span class="b2">{last}</span>'

    # Probability ring.
    on_arc = round(pct / 100 * RING_CIRCUMFERENCE, 2)
    ring = (
        '<svg class="ring" viewBox="0 0 120 120">'
        '<circle cx="60" cy="60" r="46" fill="none" stroke="var(--c1)" stroke-width="11"/>'
        '<circle cx="60" cy="60" r="46" fill="none" stroke="var(--klein)" stroke-width="11" '
        f'stroke-dasharray="{on_arc} {RING_CIRCUMFERENCE}" stroke-linecap="butt" transform="rotate(-90 60 60)"/>'
        f'<text class="rtxt" x="60" y="68" text-anchor="middle">{pct}%</text>'
        '</svg>'
    )

    return f"""  <!-- HEADER / MASTHEAD -->
  <header class="mast band">
    <div class="mast-main">
      <div class="mast-eye"><span class="dot"></span>ACTIVE RACE<span class="sub">/ {name} &middot; {distance:g} miles</span></div>
      <h1 class="title">{title_html}</h1>
      <div class="athlete">
        <span class="nm">{athlete}</span>
        <span class="meta">Goal {goal_time} - {goal_pace} /mi target</span>
      </div>
      <div class="mast-stats">
        <div class="kpi"><div class="k-lbl">Goal Time</div><div class="k-val">{goal_time}</div></div>
        <div class="kpi"><div class="k-lbl">Target Pace</div><div class="k-val">{goal_pace}<small>/mi</small></div></div>
        <div class="kpi"><div class="k-lbl">Predicted</div><div class="k-val" style="color:var(--klein)">{pred}</div></div>
        <div class="kpi">
          <div class="k-lbl">Goal Prob.</div>
          {ring}
        </div>
      </div>
    </div>
    <aside class="mast-count">
      <div>
        <div class="c-eye">Countdown</div>
        <div class="count-big"><span class="num">{days}</span><span class="days">days<br>to race</span></div>
      </div>
      <div class="race-day">
        <span class="rd-l">Race day</span>
        <span class="rd-v">{race_day}<small>{weekday}</small></span>
      </div>
    </aside>
  </header>"""


def _line_chart(series: list) -> str:
    """Inline-SVG dual line chart (ctl + atl) with faint area fill under ctl.

    Normalizes the 90-day series into a 0..320 x / 0..96 y viewBox (y inverted),
    scaled to the combined min/max of ctl + atl.
    """
    vb_w, vb_h = 320.0, 96.0
    pad_x, pad_y = 8.0, 6.0
    if not series:
        return f'<svg class="line-chart" viewBox="0 0 {vb_w:g} {vb_h:g}" preserveAspectRatio="none"></svg>'

    ctl = [float(s.get("ctl", 0)) for s in series]
    atl = [float(s.get("atl", 0)) for s in series]
    combined = ctl + atl
    lo, hi = min(combined), max(combined)
    if hi - lo < 1e-9:
        hi = lo + 1.0
    n = len(series)

    def x_at(i: int) -> float:
        if n <= 1:
            return pad_x
        return pad_x + (vb_w - 2 * pad_x) * (i / (n - 1))

    def y_at(v: float) -> float:
        # invert: high value -> low y
        frac = (v - lo) / (hi - lo)
        return pad_y + (vb_h - 2 * pad_y) * (1 - frac)

    ctl_pts = [(x_at(i), y_at(v)) for i, v in enumerate(ctl)]
    atl_pts = [(x_at(i), y_at(v)) for i, v in enumerate(atl)]

    def poly(pts):
        return " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)

    base_y = vb_h - pad_y
    area_d = (
        f"M {ctl_pts[0][0]:.1f},{base_y:.1f} "
        + "L " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in ctl_pts)
        + f" L {ctl_pts[-1][0]:.1f},{base_y:.1f} Z"
    )

    return (
        f'<svg class="line-chart" viewBox="0 0 {vb_w:g} {vb_h:g}" preserveAspectRatio="none">\n'
        f'        <path d="{area_d}" fill="var(--klein)" fill-opacity="0.07"/>\n'
        f'        <polyline points="{poly(atl_pts)}" fill="none" stroke="var(--c2)" stroke-width="1.6"/>\n'
        f'        <polyline points="{poly(ctl_pts)}" fill="none" stroke="var(--klein)" stroke-width="2.4"/>\n'
        f'      </svg>'
    )


def _fitness_prediction_split(fit: dict, race: dict) -> str:
    ctl = int(round(fit.get("ctl", 0)))
    atl = int(round(fit.get("atl", 0)))
    tsb_val = fit.get("tsb", 0)
    tsb = int(round(tsb_val))
    ctl_change_90 = int(round(fit.get("ctl_change_90d", 0)))
    phase = (fit.get("phase") or "").title() or "--"
    series = fit.get("series", [])

    tsb_class = " neg" if tsb_val < 0 else ""
    tsb_display = f"&minus;{abs(tsb)}" if tsb < 0 else (f"+{tsb}" if tsb > 0 else "0")
    if tsb_val < 0:
        form_sub = '<div class="fs-sub" style="color:var(--verm)">Building fatigue</div>'
    else:
        form_sub = '<div class="fs-sub">Fresh / recovering</div>'

    chart = _line_chart(series)

    # Race prediction column.
    vdot = race.get("vdot", 0)
    pred = race.get("predicted_time", "N/A")
    pct = race.get("probability_pct", 0)
    margin = int(round(race.get("margin_sec", 0)))
    verdict = race.get("verdict_text", race.get("verdict", ""))
    dist_label = race.get("distance_label", "Race")
    goal_short = race.get("goal_short", "Goal")  # e.g. "Sub-3:45"

    return f"""  <!-- FITNESS + RACE PREDICTION -->
  <section class="split s73 band">
    <!-- FITNESS -->
    <div class="col">
      <div class="eyebrow"><span class="idx">01</span><span class="lbl">Fitness &middot; Training Load</span><span class="rt">90-day window</span></div>
      <div class="fit-grid">
        <div class="fit-stat"><div class="fs-lbl"><b>Fitness</b> &middot; CTL</div><div class="fs-val">{ctl}</div><div class="fs-sub">{_signed(ctl_change_90)} in 90d</div></div>
        <div class="fit-stat"><div class="fs-lbl"><b>Fatigue</b> &middot; ATL</div><div class="fs-val">{atl}</div><div class="fs-sub">7-day load</div></div>
        <div class="fit-stat"><div class="fs-lbl"><b>Form</b> &middot; TSB</div><div class="fs-val{tsb_class}">{tsb_display}</div>{form_sub}</div>
      </div>
      <div class="chart-head">
        <div class="ph">Phase&nbsp;&nbsp;<b>{phase}</b></div>
        <div class="legend-inline">
          <span><i style="background:var(--klein)"></i>Fitness</span>
          <span><i style="background:var(--c2)"></i>Fatigue</span>
        </div>
      </div>
      {chart}
    </div>
    <!-- RACE PREDICTION -->
    <div class="col">
      <div class="eyebrow"><span class="idx">02</span><span class="lbl">Race Prediction</span></div>
      <div class="fs-lbl mono" style="font-size:10.5px;letter-spacing:.18em;text-transform:uppercase;color:rgba(10,10,10,.55);margin-bottom:10px">VDOT</div>
      <div class="pred-vdot">{vdot}</div>
      <div style="margin-top:26px">
        <div class="pred-row"><span class="pr-l">Predicted {dist_label}</span><span class="pr-v blue">{pred}</span></div>
        <div class="pred-row"><span class="pr-l">{goal_short} Prob.</span><span class="pr-v">{pct}%</span></div>
        <div class="pred-row"><span class="pr-l">Margin vs goal</span><span class="pr-v">{_signed(margin, 's')}</span></div>
      </div>
      <div class="verdict">
        <span class="vk">Verdict</span>
        {verdict}
      </div>
    </div>
  </section>"""


def _consistency_mileage_split(cons: dict, weekly: list) -> str:
    runs_per_week = cons.get("runs_per_week", 0)
    weeks_3plus_pct = int(round(cons.get("weeks_3plus_pct", 0)))
    streak = cons.get("streak", 0)
    eddington = cons.get("eddington", 0)

    cons_html = f"""      <div class="cons-grid">
        <div class="cons-cell"><div class="c-val">{runs_per_week}</div><div class="c-lbl">Runs / week<br>rolling avg</div></div>
        <div class="cons-cell"><div class="c-val">{weeks_3plus_pct}<small>%</small></div><div class="c-lbl">Weeks with<br>3+ runs</div></div>
        <div class="cons-cell"><div class="c-val">{streak}</div><div class="c-lbl">Day current<br>streak</div></div>
        <div class="cons-cell"><div class="c-val">{eddington}</div><div class="c-lbl">Eddington<br>number</div></div>
      </div>"""

    # Weekly mileage bars.
    max_mi = max((w["miles"] for w in weekly), default=0) or 1.0
    bars = []
    for i, w in enumerate(weekly):
        miles = w["miles"]
        longest = w.get("longest", 0)
        bar_pct = miles / max_mi * 100 if max_mi else 0
        if miles > 0:
            long_pct = min(longest / miles, 1.0) * 100
        else:
            long_pct = 0
        easy_pct = 100 - long_pct
        bars.append(
            '<div class="bar-col">'
            f'<div class="bar-val">{round(miles)}</div>'
            f'<div class="bar" style="height:{bar_pct:.1f}%">'
            f'<div class="seg easy" style="height:{easy_pct:.1f}%"></div>'
            f'<div class="seg long" style="height:{long_pct:.1f}%"></div>'
            '</div>'
            f'<div class="bar-wk">{i + 1}</div>'
            '</div>'
        )

    # "+X% vs wk 1"
    first_mi = weekly[0]["miles"] if weekly else 0
    last_mi = weekly[-1]["miles"] if weekly else 0
    if first_mi > 0:
        vs_first = int(round((last_mi - first_mi) / first_mi * 100))
    else:
        vs_first = 0

    return f"""  <!-- CONSISTENCY + MILEAGE -->
  <section class="split s37 band">
    <!-- CONSISTENCY -->
    <div class="col">
      <div class="eyebrow"><span class="idx">03</span><span class="lbl">Consistency</span></div>
{cons_html}
    </div>
    <!-- WEEKLY MILEAGE -->
    <div class="col">
      <div class="eyebrow"><span class="idx">04</span><span class="lbl">Weekly Mileage</span><span class="rt">Last 12 weeks &middot; miles</span></div>
      <div class="mile-chart">{"".join(bars)}</div>
      <div class="mile-foot">
        <span><i style="background:var(--c3)"></i>Easy / workout volume</span>
        <span><i style="background:var(--klein)"></i>Long run</span>
        <span style="margin-left:auto;color:var(--ink);font-weight:700">{_signed(vs_first, '%')} vs wk 1</span>
      </div>
    </div>
  </section>"""


def _best_efforts_table(best_efforts: dict) -> str:
    order = [("1mi", "1&nbsp;MI"), ("5K", "5K"), ("10K", "10K"),
             ("10mi", "10&nbsp;MI"), ("HM", "HALF")]
    rows = []
    for n, (key, label) in enumerate(order, start=1):
        b = best_efforts.get(key)
        if b:
            time_str = b.get("time_str", "--")
            pace = b.get("pace_str", "")
            datestr = _short_date(b.get("date"))
            rows.append(
                "        <tr>\n"
                f'          <td class="ev-idx">{n:02d}</td>\n'
                f'          <td class="ev-dist">{label}</td>\n'
                f'          <td class="ev-time">{time_str}</td>\n'
                f'          <td class="ev-pace">{pace} /mi</td>\n'
                f'          <td class="ev-date">{datestr}</td>\n'
                "        </tr>"
            )
        else:
            rows.append(
                "        <tr>\n"
                f'          <td class="ev-idx">{n:02d}</td>\n'
                f'          <td class="ev-dist">{label}</td>\n'
                '          <td class="ev-time muted">&mdash;</td>\n'
                '          <td class="ev-pace">&mdash;</td>\n'
                '          <td class="ev-date">&mdash;</td>\n'
                "        </tr>"
            )
    return (
        '      <table class="eff-table"><tbody>\n'
        + "\n".join(rows)
        + "</tbody></table>"
    )


def _plan_progress_col(data: dict, active_info: dict, today: date) -> str:
    """Return the inner HTML of the Plan Progress column, or '' to omit it."""
    # --- Structured (marathon-style) plan ---
    if config.has_structured_plan(active_info) and data.get("marathon_plan"):
        mp = data["marathon_plan"]
        phases = mp.get("phases", [])
        weeks = mp.get("weeks", [])
        cur_num = mp.get("current_week_num", 0)
        n_weeks = len(weeks)
        tw = data.get("this_week") or {}
        target = tw.get("target_miles", 0)
        actual = tw.get("miles_actual", 0)
        block_label = f"{n_weeks}-week block"

        spans = [max(1, p["end_week"] - p["start_week"] + 1) for p in phases]
        grid_cols = " ".join(f"{s}fr" for s in spans) or "1fr"

        phase_cells = []
        for p in phases:
            sw, ew = p["start_week"], p["end_week"]
            if cur_num and sw <= cur_num <= ew:
                klass = "phase cur"
            elif cur_num and ew < cur_num:
                klass = "phase done"
            else:
                klass = "phase"
            phase_cells.append(
                f'<div class="{klass}"><div class="ph-bar"></div>'
                f'<div class="ph-lbl">{p["name"]}</div>'
                f'<div class="ph-wk">Wk {sw}&ndash;{ew}</div></div>'
            )

        wp_pct = min(actual / target * 100, 100) if target else 0

        inner = f"""      <div class="eyebrow"><span class="idx">06</span><span class="lbl">Plan Progress</span><span class="rt">{block_label}</span></div>
      <div class="plan-top">
        <div class="pt-wk">Week {cur_num} <small>of {n_weeks}</small></div>
        <div class="pt-mi"><b>{target:g} mi</b> planned<br><b>{actual:g} mi</b> done</div>
      </div>
      <div class="phases" style="grid-template-columns:{grid_cols}">
        {"".join(phase_cells)}
      </div>
      <div class="wk-prog">
        <div class="wp-head"><span class="wp-l">This week &mdash; volume</span><span class="wp-v">{actual:g} / {target:g} mi</span></div>
        <div class="wp-track"><div class="wp-fill" style="width:{wp_pct:.1f}%"></div></div>
      </div>"""
        return inner

    # --- Generated short plan (no structured JSON) ---
    try:
        import planner
        plan = planner.generate_half_plan()
        weeks_list = list(getattr(plan, "weeks", None) or [])
        if not weeks_list:
            return ""
        n_weeks = len(weeks_list)

        cur_num = 0
        for w in weeks_list:
            ws = getattr(w, "week_start", None)
            if ws and ws <= today < ws + timedelta(days=7):
                cur_num = getattr(w, "week_num", 0)
                break
        if not cur_num:
            # Pick nearest by week_start if today outside all windows.
            past = [w for w in weeks_list if getattr(w, "week_start", today) <= today]
            cur_num = getattr(past[-1], "week_num", 0) if past else 0

        # Group contiguous weeks into phase bars.
        phase_groups = []  # (phase, start_num, end_num)
        for w in weeks_list:
            ph = getattr(w, "phase", "") or ""
            wn = getattr(w, "week_num", 0)
            if phase_groups and phase_groups[-1][0] == ph:
                phase_groups[-1] = (ph, phase_groups[-1][1], wn)
            else:
                phase_groups.append((ph, wn, wn))

        spans = [max(1, e - s + 1) for _, s, e in phase_groups]
        grid_cols = " ".join(f"{x}fr" for x in spans) or "1fr"
        cells = []
        for ph, s, e in phase_groups:
            if cur_num and s <= cur_num <= e:
                klass = "phase cur"
            elif cur_num and e < cur_num:
                klass = "phase done"
            else:
                klass = "phase"
            wk_txt = f"Wk {s}" if s == e else f"Wk {s}&ndash;{e}"
            cells.append(
                f'<div class="{klass}"><div class="ph-bar"></div>'
                f'<div class="ph-lbl">{ph.title()}</div>'
                f'<div class="ph-wk">{wk_txt}</div></div>'
            )

        return f"""      <div class="eyebrow"><span class="idx">06</span><span class="lbl">Plan Progress</span><span class="rt">{n_weeks}-week block</span></div>
      <div class="plan-top">
        <div class="pt-wk">Week {cur_num} <small>of {n_weeks}</small></div>
      </div>
      <div class="phases" style="grid-template-columns:{grid_cols}">
        {"".join(cells)}
      </div>"""
    except Exception:
        return ""


def _best_efforts_plan_split(data: dict, active_info: dict, today: date) -> str:
    eff = _best_efforts_table(data["best_efforts"])
    try:
        plan_col_inner = _plan_progress_col(data, active_info, today)
    except Exception:
        plan_col_inner = ""

    if plan_col_inner:
        return f"""  <!-- BEST EFFORTS + PLAN PROGRESS -->
  <section class="split s55 band">
    <!-- BEST EFFORTS -->
    <div class="col">
      <div class="eyebrow"><span class="idx">05</span><span class="lbl">Best Efforts</span><span class="rt">Recent PRs</span></div>
{eff}
    </div>
    <!-- PLAN PROGRESS -->
    <div class="col">
{plan_col_inner}
    </div>
  </section>"""

    # No plan column resolvable: Best Efforts spans full width.
    return f"""  <!-- BEST EFFORTS -->
  <section class="pad band">
    <div class="eyebrow"><span class="idx">05</span><span class="lbl">Best Efforts</span><span class="rt">Recent PRs</span></div>
{eff}
  </section>"""


def _heatmap(daily_tss: dict, today: date) -> str:
    """13-week (91-day) daily TSS heatmap, 7 rows Mon..Sun, column-major."""
    days = 91
    start = today - timedelta(days=days - 1)
    start = start - timedelta(days=start.weekday())  # align to Monday

    cells = []
    total = 0.0
    n_runs = 0
    n_rest = 0
    months = []  # (col_index, MON)
    last_month = None
    col = 0
    cur = start
    while cur <= today:
        # month header: first column where a new month appears
        if cur.weekday() == 0:
            col = (cur - start).days // 7
        if cur.month != last_month:
            months.append((col, cur.strftime("%b").upper()))
            last_month = cur.month

        tss = daily_tss.get(cur, 0)
        if tss <= 0:
            level = "l0"
            n_rest += 1
        elif tss < 30:
            level = "l1"
        elif tss < 60:
            level = "l2"
        elif tss < 100:
            level = "l3"
        elif tss < 150:
            level = "l4"
        else:
            level = "l5"
        if tss > 0:
            n_runs += 1
            total += tss
        cells.append(f'<div class="hc {level}"></div>')
        cur += timedelta(days=1)

    months_html = "".join(
        f'<span style="grid-column:{c + 1}">{m}</span>' for c, m in months
    )
    days_html = (
        '<span>M</span><span></span><span>W</span><span></span>'
        '<span>F</span><span></span><span>S</span>'
    )

    return f"""  <!-- HEATMAP -->
  <section class="pad band">
    <div class="eyebrow"><span class="idx">07</span><span class="lbl">Training Load &middot; Heatmap</span><span class="rt">Last 13 weeks &middot; daily stress</span></div>
    <div class="heat-wrap">
      <div class="heat-months">{months_html}</div>
      <div class="heat-body">
        <div class="heat-days">{days_html}</div>
        <div class="heat-grid">{"".join(cells)}</div>
      </div>
      <div class="heat-foot">
        <div class="note">Total load <b>{total:,.0f} TSS</b> &middot; {n_runs} runs &middot; {n_rest} rest days</div>
        <div class="heat-legend">Less
          <span class="sw" style="background:var(--c1)"></span>
          <span class="sw" style="background:var(--c2)"></span>
          <span class="sw" style="background:var(--c3)"></span>
          <span class="sw" style="background:var(--c4)"></span>
          <span class="sw" style="background:var(--c5)"></span>
        More</div>
      </div>
    </div>
  </section>"""


def _footer(race_name: str, today: date) -> str:
    datestr = today.strftime("%b %d, %Y")
    return f"""  <!-- FOOTER -->
  <footer class="foot">
    <div class="fl">Generated by <b>strava-run-coach</b> &middot; {race_name}</div>
    <div class="fr">{datestr}</div>
  </footer>"""


# ---------------------------------------------------------------------------
# Data assembly
# ---------------------------------------------------------------------------

def _assemble_data() -> dict:
    """Pull everything needed for the dashboard into a single dict."""
    from metrics import (load_activities, eddington_progress, current_streak,
                          longest_streak, weeks_with_3plus_runs,
                          compute_best_efforts, year_summary, rolling_year_summary,
                          fmt_pace_min_per_mi, fmt_time)

    runs = load_activities(activity_type="Run")

    # Daily TSS for heatmap
    daily_tss = defaultdict(float)
    for r in runs:
        tss = r.get("_run_tss", 0)
        if not tss:
            try:
                from enrichment import compute_run_tss
                tss = compute_run_tss(dict(r)).get("_run_tss", 0)
            except Exception:
                tss = 0
        try:
            d = datetime.fromisoformat(
                (r.get("start_date_local") or r.get("start_date", "")).replace("Z", "+00:00")
            ).date()
            daily_tss[d] += tss
        except (ValueError, AttributeError):
            continue

    # Weekly mileage (last 12 weeks, oldest -> newest)
    today = date.today()
    weeks_data = []
    for wb in range(11, -1, -1):
        wk_start = today - timedelta(days=today.weekday()) - timedelta(weeks=wb)
        wk_end = wk_start + timedelta(days=7)
        wk_runs = []
        for r in runs:
            try:
                d = datetime.fromisoformat(
                    (r.get("start_date_local") or r.get("start_date", "")).replace("Z", "+00:00")
                ).date()
            except (ValueError, AttributeError):
                continue
            if wk_start <= d < wk_end:
                wk_runs.append(r)
        miles = sum((r.get("distance", 0) or 0) / 1609.34 for r in wk_runs)
        longest = max(((r.get("distance", 0) or 0) / 1609.34 for r in wk_runs), default=0)
        weeks_data.append({
            "week_start": wk_start.isoformat(),
            "short": wk_start.strftime("%m/%d"),
            "miles": miles,
            "run_count": len(wk_runs),
            "longest": longest,
        })

    # Fitness CTL/ATL/TSB
    series = []
    fit_status = {}
    try:
        from fitness_tracker import current_status
        fit_status = current_status() or {}
        series = fit_status.get("series", []) if "series" in fit_status else []
    except Exception:
        fit_status = {}

    ctl_change_90 = (series[-1]["ctl"] - series[0]["ctl"]) if series else 0
    fitness = {
        "ctl": fit_status.get("ctl", 0),
        "atl": fit_status.get("atl", 0),
        "tsb": fit_status.get("tsb", 0),
        "phase": fit_status.get("phase", ""),
        "ctl_change_30d": fit_status.get("ctl_change_30d", 0),
        "ctl_change_90d": ctl_change_90,
        "series": series,
    }

    # Best efforts
    best_efforts = compute_best_efforts(runs)

    # Year summaries (kept for completeness / future use)
    years = year_summary(runs)
    rolling = rolling_year_summary(runs)

    # Consistency
    e = eddington_progress(runs)
    streak = current_streak(runs)
    longest = longest_streak(runs)
    w3 = weeks_with_3plus_runs(runs, weeks_window=52)

    run_counts = [w["run_count"] for w in weeks_data]
    runs_per_week = round(sum(run_counts) / len(run_counts), 1) if run_counts else 0.0
    consistency = {
        "runs_per_week": runs_per_week,
        "weeks_3plus_pct": w3.get("pct", 0),
        "recent_4_of_4": w3.get("recent_4_weeks_3plus", 0),
        "streak": streak,
        "eddington": e.get("current", 0),
    }

    # Race-aware resolution (generic via config)
    active_info = config.active_race(today)
    active_id = active_info["id"]
    race_d = date.fromisoformat(active_info["date"])
    days_to_race = (race_d - today).days
    race_distance_m = active_info["distance_mi"] * 1609.34
    distance_mi = active_info["distance_mi"]

    # Distance label for the prediction row.
    if abs(distance_mi - 26.2) < 0.3:
        distance_label = "Marathon"
    elif abs(distance_mi - 13.1) < 0.3:
        distance_label = "Half"
    elif abs(distance_mi - 6.21) < 0.3:
        distance_label = "10K"
    elif abs(distance_mi - 3.11) < 0.2:
        distance_label = "5K"
    else:
        distance_label = f"{distance_mi:g} mi"

    # Generic goal short label: "Sub-3:45" from "3:45:00", "Sub-1:59" from "1:59:59".
    goal_short = f"Sub-{active_info['goal_time'].rsplit(':', 1)[0]}"

    try:
        from race_predictor import (load_activities as rp_load,
                                     current_fitness_vdot, predict_race_time,
                                     compute_vdot, race_probability,
                                     fitness_trend, weekly_volume_mi,
                                     fmt_time as rp_fmt_time)
        rp_acts = rp_load()
        cur_v, _src = current_fitness_vdot(rp_acts)
        goal_pace_sec = active_info["goal_pace_min_per_mi"] * 60
        goal_time_sec = int(goal_pace_sec * distance_mi)
        goal_v = compute_vdot(race_distance_m, goal_time_sec)
        trend = fitness_trend(rp_acts)
        vol = weekly_volume_mi(rp_acts, days=14)
        pred_sec = predict_race_time(cur_v, race_distance_m)
        prob = race_probability(cur_v, goal_v, trend, vol)
        verdict = ("LOW" if prob < 0.40 else "MODERATE" if prob < 0.65 else "STRONG")
        verdict_text = ("Long odds. Volume must climb, now."
                        if prob < 0.40 else
                        "In reach. Hold the volume and execute."
                        if prob < 0.65 else
                        "On track. Hold the volume and you clear it.")
        pred_pace_sec = pred_sec / distance_mi
        pred_pace = f"{int(pred_pace_sec // 60)}:{int(pred_pace_sec % 60):02d}/mi"
        margin_sec = config.goal_time_to_sec(active_info["goal_time"]) - pred_sec

        race_info = {
            "race_name": active_info["name"],
            "race_date_str": race_d.strftime("%b %d, %Y"),
            "race_day_str": f'{race_d.strftime("%b")} {race_d.day}',
            "race_weekday": race_d.strftime("%a").upper(),
            "days_to_race": days_to_race,
            "distance_mi": distance_mi,
            "distance_label": distance_label,
            "probability_pct": int(round(prob * 100)),
            "predicted_time": rp_fmt_time(pred_sec),
            "predicted_pace": pred_pace,
            "verdict": verdict,
            "verdict_text": verdict_text,
            "vdot": round(cur_v, 1),
            "goal_time": active_info["goal_time"],
            "goal_pace_min_per_mi": active_info["goal_pace_min_per_mi"],
            "goal_short": goal_short,
            "margin_sec": margin_sec,
            "athlete_name": config.athlete_name(),
        }
    except Exception:
        race_info = {
            "race_name": active_info["name"],
            "race_date_str": race_d.strftime("%b %d, %Y"),
            "race_day_str": f'{race_d.strftime("%b")} {race_d.day}',
            "race_weekday": race_d.strftime("%a").upper(),
            "days_to_race": days_to_race,
            "distance_mi": distance_mi,
            "distance_label": distance_label,
            "probability_pct": 0,
            "predicted_time": "N/A",
            "predicted_pace": "",
            "verdict": "no data",
            "verdict_text": "Not enough data to forecast yet.",
            "vdot": 0,
            "goal_time": active_info["goal_time"],
            "goal_pace_min_per_mi": active_info["goal_pace_min_per_mi"],
            "goal_short": goal_short,
            "margin_sec": 0,
            "athlete_name": config.athlete_name(),
        }

    # Structured marathon plan data (only when active race uses a JSON plan).
    marathon_plan_data = None
    this_week_data = None

    if config.has_structured_plan(active_info):
        try:
            import marathon_plan as _mp
            from plan_tracker import weekly_compliance

            cw = _mp.current_week(today)
            current_week_num = cw["week_num"] if cw else 0

            marathon_plan_data = {
                "weeks": _mp.all_weeks(),
                "phases": _mp.all_phases(),
                "current_week_num": current_week_num,
            }

            if cw:
                wc = weekly_compliance(cw["week_num"])
                phase = _mp.phase_by_id(cw["phase"])
                this_week_data = {
                    "week_num": cw["week_num"],
                    "phase_name": phase["name"] if phase else cw["phase"],
                    "target_miles": wc.get("target_miles", 0),
                    "miles_actual": wc.get("miles_actual", 0),
                    "long_run_target": wc.get("long_run_target", 0),
                    "long_run_actual": wc.get("long_run_actual", 0),
                    "status": wc.get("status", "in_progress"),
                }
        except Exception as ex:
            print(f"  [dashboard] Marathon plan data unavailable: {ex}")

    return {
        "race_info": race_info,
        "active_race_id": active_id,
        "active_race_info": active_info,
        "daily_tss": dict(daily_tss),
        "weekly": weeks_data,
        "fitness": fitness,
        "best_efforts": best_efforts,
        "years": years,
        "rolling": rolling,
        "eddington": e,
        "streak": streak,
        "longest_streak": longest,
        "weeks_3plus": w3,
        "consistency": consistency,
        "marathon_plan": marathon_plan_data,
        "this_week": this_week_data,
    }


# ---------------------------------------------------------------------------
# HTML wrapper
# ---------------------------------------------------------------------------

def _build_html(data: dict) -> str:
    t = config.theme()
    today = date.today()
    race = data["race_info"]
    active_info = data["active_race_info"]

    css = _build_css(t)
    disp = t["display_font"]
    mono = t["mono_font"]

    sections = "\n\n".join([
        _topbar(),
        _masthead(race),
        _fitness_prediction_split(data["fitness"], race),
        _consistency_mileage_split(data["consistency"], data["weekly"]),
        _best_efforts_plan_split(data, active_info, today),
        _heatmap(data["daily_tss"], today),
        _footer(race["race_name"], today),
    ])

    fonts_link = (
        '<link rel="preconnect" href="https://fonts.googleapis.com">\n'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
        f'<link href="https://fonts.googleapis.com/css2?family={disp.replace(" ", "+")}:wght@400;500;700'
        f'&family={mono.replace(" ", "+")}:wght@400;700&display=swap" rel="stylesheet">'
    )

    title = f"strava-run-coach &mdash; {race['race_name']} Training Report"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
{fonts_link}
{css}
</head>
<body>
<div class="poster">

{sections}

</div>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def generate_dashboard(open_browser: bool = False) -> Path:
    """Generate dashboard.html. Returns path."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    data = _assemble_data()
    html = _build_html(data)
    OUT_FILE.write_text(html, encoding="utf-8")
    if open_browser:
        import webbrowser
        webbrowser.open(OUT_FILE.as_uri())
    return OUT_FILE


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    open_browser = "--open" in sys.argv
    path = generate_dashboard(open_browser=open_browser)
    print(f"Dashboard written to: {path}")


if __name__ == "__main__":
    main()
