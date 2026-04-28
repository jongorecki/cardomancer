"""
Generate an interactive HTML foil review page for a sort session.

For each borderline card (|foil_confidence| < threshold), shows the two
card crop images (normal lighting vs foil-probe lighting) flickering back
and forth so you can visually judge whether the card is foil.

Usage:
    python generate_foil_review.py <session_dir> [--threshold 0.5]

Output:
    foil_review_<session>.html  — open in any browser
    Apply verdicts afterwards with:
    python apply_foil_verdicts.py <session_dir> foil_verdicts.csv
"""

import argparse
import csv
import html as html_mod
import os
import sys
import webbrowser

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def file_url(path):
    return "file:///" + os.path.abspath(path).replace("\\", "/")


def run(session_dir, threshold):
    csv_path = os.path.join(session_dir, "scans.csv")
    if not os.path.exists(csv_path):
        print(f"ERROR: {csv_path} not found")
        sys.exit(1)

    rows = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if not r.get("foil_confidence") or r.get("recognized") != "True":
                continue
            if abs(float(r["foil_confidence"])) < threshold:
                rows.append(r)

    rows.sort(key=lambda r: abs(float(r["foil_confidence"])))  # most uncertain first

    session_name = os.path.basename(os.path.normpath(session_dir))
    out_path = os.path.join(SCRIPT_DIR, f"foil_review_{session_name}.html")

    card_rows_html = []
    for r in rows:
        n = int(r["scan_num"])
        img_a = file_url(os.path.join(session_dir, "card_crops",   f"card_{n:04d}.jpg"))
        img_b = file_url(os.path.join(session_dir, "card_crops_b", f"card_{n:04d}.jpg"))
        name  = html_mod.escape(r.get("name", ""))
        conf  = float(r["foil_confidence"])
        cur   = "foil" if r.get("is_foil") in ("1", "True", "true") else "nonfoil"
        cur_label = "🟡 foil" if cur == "foil" else "⚪ nonfoil"

        card_rows_html.append(f"""
    <div class="card-row" id="row-{n}">
      <div class="card-img-wrap">
        <img class="img-a active" src="{img_a}" alt="normal">
        <img class="img-b" src="{img_b}" alt="foil-probe">
        <div class="img-label">
          <span class="lbl-a active-lbl">normal</span>
          <span class="lbl-b">probe</span>
        </div>
      </div>
      <div class="card-info">
        <div class="card-num">#{n}</div>
        <div class="card-name">{name}</div>
        <div class="card-meta">conf: {conf:+.3f} &nbsp;|&nbsp; current: {cur_label}</div>
        <div class="verdict-btns">
          <button class="btn-foil"    onclick="setVerdict({n}, 'foil')">Foil</button>
          <button class="btn-nonfoil" onclick="setVerdict({n}, 'nonfoil')">Non-foil</button>
          <button class="btn-skip"    onclick="setVerdict({n}, 'skip')">Skip</button>
        </div>
        <div class="verdict-display" id="verdict-{n}"></div>
      </div>
    </div>""")

    cards_html = "\n".join(card_rows_html)
    total = len(rows)

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Foil Review — {html_mod.escape(session_name)}</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; }}
  body {{ font-family: system-ui, -apple-system, sans-serif;
         background: #1a1a1a; color: #eee; margin: 0; padding: 16px; }}
  h1 {{ margin: 0 0 4px; font-size: 1.3rem; }}
  .subtitle {{ color: #888; font-size: 0.85rem; margin-bottom: 16px; }}

  .controls {{ display: flex; align-items: center; gap: 16px;
               background: #252525; padding: 10px 14px;
               border-radius: 6px; margin-bottom: 16px; flex-wrap: wrap; }}
  .controls label {{ font-size: 0.85rem; color: #bbb; }}
  .controls input[type=range] {{ width: 120px; }}
  #progress {{ font-size: 0.9rem; color: #aaa; }}
  #export-btn {{ margin-left: auto; padding: 7px 16px; background: #4a9;
                 color: #000; border: none; border-radius: 4px;
                 font-weight: bold; cursor: pointer; font-size: 0.9rem; }}
  #export-btn:hover {{ background: #5ba; }}

  .grid {{ display: flex; flex-wrap: wrap; gap: 12px; }}

  .card-row {{ background: #252525; border-radius: 8px; padding: 10px;
               display: flex; gap: 10px; align-items: flex-start;
               width: 320px; transition: background 0.2s; }}
  .card-row.voted-foil    {{ border-left: 3px solid #fc6; }}
  .card-row.voted-nonfoil {{ border-left: 3px solid #4a9; }}
  .card-row.voted-skip    {{ border-left: 3px solid #555; opacity: 0.5; }}

  .card-img-wrap {{ position: relative; width: 140px; flex-shrink: 0; }}
  .card-img-wrap img {{ width: 140px; border-radius: 5px;
                        border: 1px solid #444; display: block;
                        position: absolute; top: 0; left: 0;
                        opacity: 0; transition: opacity 0.08s; }}
  .card-img-wrap img.active {{ opacity: 1; position: relative; }}
  /* reserve height so the wrap doesn't collapse */
  .card-img-wrap {{ min-height: 196px; }}

  .img-label {{ display: flex; gap: 8px; margin-top: 4px;
                font-size: 0.7rem; color: #666; }}
  .img-label .active-lbl {{ color: #adf; }}

  .card-info {{ flex: 1; min-width: 0; }}
  .card-num  {{ font-size: 0.75rem; color: #777; }}
  .card-name {{ font-weight: bold; font-size: 0.9rem;
                white-space: nowrap; overflow: hidden;
                text-overflow: ellipsis; margin-bottom: 2px; }}
  .card-meta {{ font-size: 0.72rem; color: #888; margin-bottom: 8px; }}

  .verdict-btns {{ display: flex; flex-direction: column; gap: 4px; }}
  .verdict-btns button {{ padding: 5px 10px; border: none; border-radius: 4px;
                          cursor: pointer; font-size: 0.8rem; font-weight: bold;
                          text-align: left; }}
  .btn-foil    {{ background: #5a3; color: #fff; }}
  .btn-nonfoil {{ background: #358; color: #fff; }}
  .btn-skip    {{ background: #444; color: #aaa; }}
  .btn-foil:hover    {{ background: #6b4; }}
  .btn-nonfoil:hover {{ background: #469; }}
  .btn-skip:hover    {{ background: #555; }}

  .verdict-display {{ margin-top: 6px; font-size: 0.75rem; font-weight: bold; color: #fc6; }}
</style>
</head>
<body>
<h1>Foil Review — {html_mod.escape(session_name)}</h1>
<div class="subtitle">{total} borderline cards (|foil_confidence| &lt; {threshold}) — most uncertain first</div>

<div class="controls">
  <label>Flicker speed:
    <input type="range" id="speed-slider" min="100" max="1500" value="500" step="50"
           oninput="setSpeed(this.value)">
    <span id="speed-label">500ms</span>
  </label>
  <span id="progress">0 / {total} reviewed</span>
  <button id="export-btn" onclick="exportCSV()">Export verdicts CSV</button>
</div>

<div class="grid">
{cards_html}
</div>

<script>
const verdicts = {{}};
const total = {total};

function setVerdict(n, v) {{
  verdicts[n] = v;
  const row = document.getElementById('row-' + n);
  row.className = 'card-row voted-' + v;
  const disp = document.getElementById('verdict-' + n);
  disp.textContent = v === 'foil' ? '✔ foil' : v === 'nonfoil' ? '✔ non-foil' : '— skipped';
  document.getElementById('progress').textContent =
    Object.keys(verdicts).length + ' / {total} reviewed';
}}

// Flicker all card image pairs
let flickerInterval = null;
let showingB = false;

function flicker() {{
  showingB = !showingB;
  document.querySelectorAll('.card-img-wrap').forEach(wrap => {{
    const a = wrap.querySelector('.img-a');
    const b = wrap.querySelector('.img-b');
    const la = wrap.querySelector('.lbl-a');
    const lb = wrap.querySelector('.lbl-b');
    if (showingB) {{
      a.classList.remove('active'); b.classList.add('active');
      la.classList.remove('active-lbl'); lb.classList.add('active-lbl');
    }} else {{
      b.classList.remove('active'); a.classList.add('active');
      lb.classList.remove('active-lbl'); la.classList.add('active-lbl');
    }}
  }});
}}

function setSpeed(ms) {{
  document.getElementById('speed-label').textContent = ms + 'ms';
  clearInterval(flickerInterval);
  flickerInterval = setInterval(flicker, parseInt(ms));
}}

setSpeed(500);

function exportCSV() {{
  const lines = ['scan_num,verdict'];
  Object.entries(verdicts)
    .sort((a, b) => parseInt(a[0]) - parseInt(b[0]))
    .forEach(([n, v]) => lines.push(n + ',' + v));
  const blob = new Blob([lines.join('\\n')], {{type: 'text/csv'}});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'foil_verdicts.csv';
  a.click();
}}
</script>
</body></html>"""

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Generated {out_path}")
    print(f"  {total} cards to review")
    print(f"\nAfter reviewing, export the CSV from the page and run:")
    print(f"  python apply_foil_verdicts.py {session_dir} foil_verdicts.csv")

    try:
        webbrowser.open(file_url(out_path))
    except Exception:
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("session_dir")
    parser.add_argument("--threshold", type=float, default=0.5)
    args = parser.parse_args()
    run(args.session_dir, args.threshold)
