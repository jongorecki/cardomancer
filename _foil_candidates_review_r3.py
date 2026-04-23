"""Round 3: review candidates under the NEW Level-2 5-feature model.

Two groups to surface:
  (A) DISPUTED — scans labeled 'nonfoil' in round 1 but scoring very high
      (>+2.0) under the new model. Strong evidence the original nonfoil
      verdict may have been wrong; worth a second look with fresh eyes.
  (B) NEW CANDIDATES — scans never reviewed in rounds 1/2, sorted by the
      shipped 5-feature confidence. Top 15.

Output: foil_candidates_review_r3.html with radio buttons and CSV export.
Default radio is whatever round 1 said (for group A) or 'nonfoil' (for
group B) — flip only if you can see shimmer/rainbow/specular.
"""
import os
import sqlite3
import html

import cv2

from cards import CARDS_DATA
from foil_detect import detect_foil

# ---------------------------------------------------------------------------
# Prior verdicts (from _foil_tune.py header comments)

ROUND1_FOIL = {32, 105, 44, 23, 188, 25, 144, 52, 82, 1}
ROUND1_NONFOIL = {2, 6, 14, 8, 133}
ROUND2_FOIL = {42, 299}
ROUND2_NONFOIL = {22, 3, 90, 187, 20, 234, 46, 196, 4, 108, 189,
                  83, 92, 217, 39, 49, 169, 15, 50, 51, 139, 54, 12}

ALREADY_FOIL = ROUND1_FOIL | ROUND2_FOIL
ALREADY_NONFOIL = ROUND1_NONFOIL | ROUND2_NONFOIL
ALREADY_REVIEWED = ALREADY_FOIL | ALREADY_NONFOIL

# Score threshold above which a round-1 "nonfoil" verdict is worth
# re-examining under the Level-2 model.
DISPUTE_THRESHOLD = 2.0

# ---------------------------------------------------------------------------
# Paths

SESSION_ID = 44
CROP_DIR = r"D:\Card_Sorter\Scripts\scan_logs\session_20260421_140152\card_crops"
SCAN_DIR = r"D:\Card_Sorter\Scripts\scan_logs\session_20260421_140152\scan_images"
REF_DIR = r"D:\Card_Sorter\Scripts\downloaded_cards"
OUT_HTML = r"D:\Card_Sorter\Scripts\foil_candidates_review_r3.html"
N_NEW_TO_SHOW = 15

# ---------------------------------------------------------------------------
# Card lookup index

idx = {}
for c_ in CARDS_DATA:
    key = (c_.get("set", "").lower(), c_.get("collector_number", ""))
    idx.setdefault(key, c_.get("id"))

# ---------------------------------------------------------------------------
# Run the SHIPPED Level-2 detector on every session 44 crop. No refitting
# here — we trust the weights pinned in foil_detect.py.

c = sqlite3.connect(r"D:\Card_Sorter\Scripts\collection.db")
cur = c.cursor()
cur.execute("""
    SELECT scan_num, name, set_code, collector_number
    FROM scan_history WHERE session_id = ? ORDER BY scan_num
""", (SESSION_ID,))
rows = cur.fetchall()
c.close()

candidates = []
for scan_num, name, set_code, num in rows:
    crop = os.path.join(CROP_DIR, f"card_{scan_num:04d}.jpg")
    if not os.path.isfile(crop):
        continue
    img = cv2.imread(crop)
    if img is None:
        continue
    card_id = idx.get(((set_code or "").lower(), num or ""))
    r = detect_foil(img, card_id=card_id)
    if r["reason"] != "ok":
        continue
    sg = r["signals"]
    candidates.append({
        "scan": scan_num,
        "name": name,
        "set_code": set_code,
        "num": num,
        "card_id": card_id,
        "dbf": sg["delta_bright_frac"],
        "dms": sg["delta_mean_s"],
        "dhr": sg["delta_hue_range"],
        "dnc": sg["delta_n_bright_clusters"],
        "dss": sg["delta_std_s_bright"],
        "dle": sg["delta_laplacian_energy"],
        "confidence": r["confidence"],
    })

candidates.sort(key=lambda r: r["confidence"], reverse=True)

# ---------------------------------------------------------------------------
# Group A: disputed round-1 nonfoils (now scoring > DISPUTE_THRESHOLD)
# Group B: fresh candidates never reviewed, top N by confidence.

group_a_disputed = [
    c for c in candidates
    if c["scan"] in ROUND1_NONFOIL and c["confidence"] >= DISPUTE_THRESHOLD
]

group_b_new = []
for cand in candidates:
    if cand["scan"] in ALREADY_REVIEWED:
        continue
    group_b_new.append(cand)
    if len(group_b_new) >= N_NEW_TO_SHOW:
        break

print(f"Group A — disputed round-1 nonfoils (score >= {DISPUTE_THRESHOLD}): "
      f"{len(group_a_disputed)}")
for c_ in group_a_disputed:
    print(f"  scan={c_['scan']:>3}  conf={c_['confidence']:+.3f}  "
          f"{(c_['name'] or '')[:50]}")

print(f"\nGroup B — new candidates (top {N_NEW_TO_SHOW} unreviewed): "
      f"{len(group_b_new)}")
for c_ in group_b_new:
    print(f"  scan={c_['scan']:>3}  conf={c_['confidence']:+.3f}  "
          f"{(c_['name'] or '')[:50]}")

# ---------------------------------------------------------------------------
# HTML rendering helpers


def f_url(p):
    return ("file:///" + p.replace("\\", "/")) if p and os.path.isfile(p) else ""


def render_row(cand, default_verdict):
    scan_num = cand["scan"]
    card_id = cand["card_id"]
    ref_path = os.path.join(REF_DIR, f"{card_id}.png") if card_id else None
    crop_path = os.path.join(CROP_DIR, f"card_{scan_num:04d}.jpg")
    scan_path = os.path.join(SCAN_DIR, f"scan_{scan_num:04d}.jpg")

    sig_str = (f"<b>dbf</b>={cand['dbf']:+.3f}<br>"
               f"<b>dms</b>={cand['dms']:+.1f}<br>"
               f"<b>dnc</b>={cand['dnc']:+.0f}<br>"
               f"<b>dss</b>={cand['dss']:+.1f}<br>"
               f"<b>dle</b>={cand['dle']:+.1f}<br>"
               f"<small><i>dhr</i>={cand['dhr']:+.1f} (unscored)</small>")

    ref_url = f_url(ref_path)
    crop_url = f_url(crop_path)
    scan_url = f_url(scan_path)

    ref_cell = (f'<img src="{ref_url}" style="max-width:280px;max-height:400px">'
                if ref_url else "<i>no reference</i>")
    crop_cell = (f'<img src="{crop_url}" style="max-width:280px;max-height:400px">'
                 if crop_url else "<i>missing crop</i>")
    scan_cell = (f'<img src="{scan_url}" style="max-width:200px;max-height:300px">'
                 if scan_url else "")

    radio_name = f"verdict_{scan_num}"

    def checked(val):
        return " checked" if default_verdict == val else ""

    radios = (
        f'<label><input type=radio name={radio_name} value=foil'
        f'{checked("foil")} onchange="updateCsv()">FOIL</label><br>'
        f'<label><input type=radio name={radio_name} value=nonfoil'
        f'{checked("nonfoil")} onchange="updateCsv()">nonfoil</label><br>'
        f'<label><input type=radio name={radio_name} value=cant_tell'
        f'{checked("cant_tell")} onchange="updateCsv()">can\'t tell</label>'
    )

    return f"""
<tr>
  <td><b>scan {scan_num}</b><br>conf {cand['confidence']:+.2f}</td>
  <td>{html.escape(cand['name'] or '')}<br>
      <small>({html.escape(cand['set_code'] or '')} #{html.escape(cand['num'] or '')})</small></td>
  <td>{sig_str}</td>
  <td>{crop_cell}</td>
  <td>{ref_cell}</td>
  <td>{scan_cell}</td>
  <td>{radios}</td>
</tr>"""


# ---------------------------------------------------------------------------
# Build HTML

rows_a = [render_row(c_, default_verdict="nonfoil") for c_ in group_a_disputed]
rows_b = [render_row(c_, default_verdict="nonfoil") for c_ in group_b_new]

dispute_count = len(group_a_disputed)
new_count = len(group_b_new)

page = f"""<!doctype html>
<html><head><meta charset=utf-8>
<title>Foil Candidates Review Round 3 (Level-2)</title>
<style>
  body {{ font-family: sans-serif; padding: 20px; }}
  h2 {{ margin-top: 40px; border-bottom: 2px solid #333; padding-bottom: 6px; }}
  table {{ border-collapse: collapse; width: 100%; margin-bottom: 20px; }}
  td, th {{ border: 1px solid #ccc; padding: 8px; vertical-align: top; }}
  th {{ background: #f0f0f0; }}
  textarea {{ width: 100%; height: 280px; font-family: monospace; }}
  .intro {{ background: #fffbe6; padding: 10px; border-left: 3px solid #e0a800;
           margin: 10px 0; }}
  .dispute {{ background: #ffecec; padding: 10px; border-left: 3px solid #c00;
              margin: 10px 0; }}
  .fresh {{ background: #e9f7ef; padding: 10px; border-left: 3px solid #2a7;
            margin: 10px 0; }}
</style>
</head><body>
<h1>Foil candidates &mdash; Round 3 (Level-2 5-feature model)</h1>

<div class=intro>
<p>The new Level-2 model adds 3 signals on top of the original 3:
<code>delta_n_bright_clusters</code>, <code>delta_std_s_bright</code>,
<code>delta_laplacian_energy</code>. It re-scored all session-44 scans and
produced two groups that need a second look.</p>
<p>Default radio in both groups is "nonfoil" &mdash; flip to FOIL only for
clear shimmer/rainbow/specular highlights.</p>
</div>

<h2>Group A &mdash; DISPUTED ({dispute_count} scans)</h2>
<div class=dispute>
<p>These were labeled <b>nonfoil</b> in round 1 but the new model scores
them above <b>+{DISPUTE_THRESHOLD:.1f}</b>. That's in the same band as
our confirmed foils. Either the original verdict was wrong, or the
model is making a confident mistake and we should understand why.</p>
<p>Take a fresh look &mdash; don't be anchored by the round-1 call.</p>
</div>

<table>
<tr><th>Scan/Conf</th><th>Card</th><th>Signals</th>
    <th>Crop</th><th>Scryfall ref</th>
    <th>Full scan frame</th><th>Verdict</th></tr>
{''.join(rows_a)}
</table>

<h2>Group B &mdash; NEW CANDIDATES ({new_count} scans)</h2>
<div class=fresh>
<p>Top {N_NEW_TO_SHOW} session-44 scans never reviewed in rounds 1 or 2,
sorted by the shipped 5-feature confidence. Scans scoring above the
shipping threshold of <b>+1.75</b> would be flagged as foils by the
detector right now.</p>
</div>

<table>
<tr><th>Scan/Conf</th><th>Card</th><th>Signals</th>
    <th>Crop</th><th>Scryfall ref</th>
    <th>Full scan frame</th><th>Verdict</th></tr>
{''.join(rows_b)}
</table>

<h2>Verdict CSV (copy when done)</h2>
<textarea id=csv readonly></textarea>

<script>
function updateCsv() {{
  const lines = ["scan,verdict"];
  document.querySelectorAll('input[type=radio]:checked').forEach(r => {{
    const scan = r.name.replace('verdict_', '');
    lines.push(scan + ',' + r.value);
  }});
  document.getElementById('csv').value = lines.join('\\n');
}}
updateCsv();
</script>
</body></html>
"""

with open(OUT_HTML, "w", encoding="utf-8") as f:
    f.write(page)

print(f"\nWrote {OUT_HTML}")
print(f"  Group A: {dispute_count} disputed round-1 nonfoils")
print(f"  Group B: {new_count} fresh candidates")
