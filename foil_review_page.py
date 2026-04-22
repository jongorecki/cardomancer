#!/usr/bin/env python3
# foil_review_page.py
# ---------------------------------------------------------------------------
# Generates foil_review.html — a single static HTML page pairing each
# top-candidate scan with its matched reference image so you can visually
# confirm the multi-signal foil detector is working.
#
# Layout (one row per scan):
#   rank | scan#  | scan image | reference image | conf | deltas | name (set #)
#
# Shows:
#   - Top 50 scans by confidence (everything at or above threshold plus a
#     margin of "borderline false positives" just below)
#   - Bottom 10 (sanity check — these should look clearly nonfoil)
#   - Known-foil marker on scan 205
#
# Run:  python foil_review_page.py
# Open: foil_review.html in any browser.
#
# Prereq: run `python prototype_foil_multisignal.py` first to generate the
# multisignal_all.csv and the scan crops under foil_multisignal/.
# ---------------------------------------------------------------------------

import os
import sys
import csv
import html
import webbrowser

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from cards import CARDS_DATA
from foil_detect import FOIL_CONFIDENCE_THRESHOLD

CSV_PATH = os.path.join(SCRIPT_DIR, "foil_multisignal",
                         "multisignal_all.csv")
SESSION_CROPS_DIR = os.path.join(
    SCRIPT_DIR, "scan_logs", "session_20260415_130451", "card_crops")
REFERENCE_DIR = os.path.join(SCRIPT_DIR, "downloaded_cards")
OUT_HTML = os.path.join(SCRIPT_DIR, "foil_review.html")

TOP_N = 50        # show this many strongest-positive candidates
BOTTOM_N = 10     # show this many strongest-negative candidates (sanity)
KNOWN_FOIL_SCAN = 205


def _resolve_card_id(name, set_code, collector_number):
    """Find a card_id for a (name, set, number) triple."""
    if not name:
        return None

    name_lower = name.lower()
    set_lower = (set_code or "").lower()

    for c in CARDS_DATA:
        if (c.get("name", "").lower() == name_lower
                and c.get("set", "").lower() == set_lower
                and str(c.get("collector_number", "")) == str(collector_number)):
            cid = c.get("id")
            if cid and os.path.isfile(os.path.join(
                    REFERENCE_DIR, f"{cid}.png")):
                return cid

    for c in CARDS_DATA:
        if (c.get("name", "").lower() == name_lower
                and c.get("set", "").lower() == set_lower):
            cid = c.get("id")
            if cid and os.path.isfile(os.path.join(
                    REFERENCE_DIR, f"{cid}.png")):
                return cid

    for c in CARDS_DATA:
        if c.get("name", "").lower() == name_lower:
            cid = c.get("id")
            if cid and os.path.isfile(os.path.join(
                    REFERENCE_DIR, f"{cid}.png")):
                return cid

    return None


def _file_url(abspath):
    """Make a file:// URL from an absolute path (Windows-safe)."""
    return "file:///" + abspath.replace("\\", "/")


def _row_html(rank, row, card_id, is_known_foil):
    scan_num = int(row["scan_num"])
    conf = float(row.get("confidence") or 0)
    is_foil_int = int(row.get("is_foil") or 0)
    name = row.get("name", "") or "(unrecognized)"
    set_code = row.get("set", "")
    db = row.get("delta_bright_frac", "")
    dm = row.get("delta_mean_s", "")
    dh = row.get("delta_hue_range", "")

    scan_path = os.path.join(
        SESSION_CROPS_DIR, f"card_{scan_num:04d}.jpg")
    if not os.path.isfile(scan_path):
        scan_url = ""
    else:
        scan_url = _file_url(scan_path)

    ref_url = ""
    if card_id:
        ref_path = os.path.join(REFERENCE_DIR, f"{card_id}.png")
        if os.path.isfile(ref_path):
            ref_url = _file_url(ref_path)

    row_class = "row"
    badges = []
    if is_foil_int:
        badges.append('<span class="badge badge-foil">FLAGGED</span>')
    if is_known_foil:
        badges.append('<span class="badge badge-known">KNOWN FOIL</span>')
        row_class += " known-foil"
    if conf >= FOIL_CONFIDENCE_THRESHOLD and not is_foil_int:
        badges.append('<span class="badge badge-border">border</span>')

    conf_class = "conf-pos" if conf >= FOIL_CONFIDENCE_THRESHOLD else "conf-neg"

    return f"""
    <tr class="{row_class}">
      <td class="rank">#{rank}</td>
      <td class="scan-num">{scan_num}</td>
      <td class="img-cell">
        {f'<img src="{scan_url}" alt="scan {scan_num}">' if scan_url else '<span class="missing">[no scan]</span>'}
        <div class="img-label">scan</div>
      </td>
      <td class="img-cell">
        {f'<img src="{ref_url}" alt="reference">' if ref_url else '<span class="missing">[no reference]</span>'}
        <div class="img-label">reference</div>
      </td>
      <td class="info">
        <div class="name">{html.escape(name)}</div>
        <div class="set">{html.escape(set_code)}</div>
        <div class="badges">{' '.join(badges)}</div>
      </td>
      <td class="conf {conf_class}">{conf:+.3f}</td>
      <td class="deltas">
        <div>&Delta;bf: <code>{db}</code></div>
        <div>&Delta;s: <code>{dm}</code></div>
        <div>&Delta;hr: <code>{dh}</code></div>
      </td>
      <td class="verdict">
        <label><input type="radio" name="v{scan_num}" value="foil"> foil</label>
        <label><input type="radio" name="v{scan_num}" value="nonfoil"> nonfoil</label>
        <label><input type="radio" name="v{scan_num}" value="unsure"> ?</label>
      </td>
    </tr>
    """


HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Foil Detection Review</title>
<style>
  body { font-family: system-ui, -apple-system, Segoe UI, sans-serif;
         background:#1e1e1e; color:#eee; margin:0; padding:20px; }
  h1 { margin-top:0; }
  .intro { max-width:900px; line-height:1.5; color:#ccc; }
  .intro code { background:#333; padding:1px 5px; border-radius:3px;
                color:#ff9; }
  .legend { margin:15px 0; padding:12px; background:#2a2a2a;
            border-left:3px solid #4a9; border-radius:4px; }
  .threshold-banner { margin:20px 0; padding:15px; background:#3a3a3a;
                      border:1px dashed #888; text-align:center;
                      font-weight:bold; color:#ff9; }
  table { border-collapse:collapse; width:100%; max-width:1400px;
          margin-top:15px; background:#252525; }
  th, td { padding:8px 10px; border-bottom:1px solid #333;
           vertical-align:top; text-align:left; }
  th { background:#333; position:sticky; top:0; z-index:1; }
  tr.known-foil { background:#2a3a2a; }
  tr.known-foil td { border-bottom-color:#4a7; }
  .rank { color:#888; font-variant-numeric:tabular-nums; }
  .scan-num { font-variant-numeric:tabular-nums; color:#aaa; }
  .img-cell { width:250px; text-align:center; }
  .img-cell img { max-width:240px; max-height:340px;
                  border:1px solid #444; border-radius:4px;
                  display:block; margin:0 auto; }
  .img-label { color:#777; font-size:11px; margin-top:3px; }
  .missing { color:#f55; font-style:italic; font-size:12px; }
  .info .name { font-weight:bold; font-size:14px; }
  .info .set { color:#aaa; font-size:12px; font-family:monospace; }
  .badges { margin-top:6px; }
  .badge { display:inline-block; padding:2px 7px; border-radius:3px;
           font-size:11px; font-weight:bold; margin-right:4px;
           margin-bottom:2px; }
  .badge-foil { background:#9a6; color:#000; }
  .badge-known { background:#4af; color:#000; }
  .badge-border { background:#d85; color:#000; }
  .conf { font-variant-numeric:tabular-nums; font-weight:bold;
          font-size:16px; text-align:right; padding-right:18px; }
  .conf-pos { color:#9f9; }
  .conf-neg { color:#f99; }
  .deltas { font-size:12px; color:#bbb; }
  .deltas code { background:#222; padding:1px 4px; border-radius:2px;
                 color:#ff9; font-size:11px; }
  .verdict label { display:block; font-size:12px; margin:2px 0;
                   cursor:pointer; }
  .verdict input { margin-right:5px; }
  .summary { margin-top:20px; padding:15px; background:#2a2a2a;
             border-left:3px solid #69c; border-radius:4px; }
  .summary button { padding:8px 15px; background:#69c; color:#000;
                    border:none; border-radius:3px; font-weight:bold;
                    cursor:pointer; }
  .summary button:hover { background:#79d; }
  .summary pre { background:#1a1a1a; padding:10px; border-radius:3px;
                 overflow-x:auto; color:#ff9; }
</style>
</head>
<body>
"""


def main():
    if not os.path.isfile(CSV_PATH):
        print(f"ERROR: {CSV_PATH} not found.")
        print("Run `python prototype_foil_multisignal.py` first.")
        sys.exit(1)

    # Load all rows
    all_rows = []
    with open(CSV_PATH, "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                conf = float(row.get("confidence") or 0)
            except ValueError:
                conf = 0.0
            row["_conf"] = conf
            all_rows.append(row)

    # Sort descending by confidence
    all_rows.sort(key=lambda r: r["_conf"], reverse=True)

    # Take top N + bottom N
    top_rows = all_rows[:TOP_N]
    bottom_rows = all_rows[-BOTTOM_N:]

    # Count statistics
    total = len(all_rows)
    flagged = sum(1 for r in all_rows
                  if int(r.get("is_foil") or 0))
    min_conf = all_rows[-1]["_conf"]
    max_conf = all_rows[0]["_conf"]

    # Find known-foil rank
    known_rank = None
    for i, r in enumerate(all_rows, start=1):
        if int(r["scan_num"]) == KNOWN_FOIL_SCAN:
            known_rank = i
            known_conf = r["_conf"]
            break

    # Resolve card_ids
    print(f"Resolving card_ids for {len(top_rows) + len(bottom_rows)} rows...")
    resolved = {}
    for r in top_rows + bottom_rows:
        cid = _resolve_card_id(
            r.get("name"), r.get("set"), None)
        resolved[int(r["scan_num"])] = cid

    # Build HTML
    parts = [HEAD]
    parts.append(f"""
    <h1>Foil Detection Review</h1>
    <div class="intro">
      <p>This page pairs each scan with its matched reference image
      so you can confirm whether the multi-signal foil detector
      (<code>foil_detect.py</code>) agrees with reality.</p>
      <p>For each row, compare the left image (<strong>scan</strong>,
      captured under the LED scanner) with the right image
      (<strong>reference</strong>, Scryfall's flat-lit photo).
      A real foil shows rainbow-colored specular hotspots in the
      scan that aren't in the reference.</p>
      <p>Tick the verdict column to informally label. Results are
      <em>not saved</em> — this is a visual sanity check only.</p>
    </div>
    <div class="legend">
      <strong>Session:</strong> session_20260415_130451 ({total} reference-comparable scans,
      {flagged} flagged at threshold {FOIL_CONFIDENCE_THRESHOLD})<br>
      <strong>Confidence range:</strong> {min_conf:+.3f} (most nonfoil-like) to
      {max_conf:+.3f} (most foil-like)<br>
      <strong>Known foil reference:</strong> scan {KNOWN_FOIL_SCAN} (Zombie Infestation) —
      rank #{known_rank}, conf {known_conf:+.3f}<br>
      <strong>Badges:</strong>
      <span class="badge badge-foil">FLAGGED</span> is_foil=1 in CSV
      &middot;
      <span class="badge badge-known">KNOWN FOIL</span> hand-confirmed reference
      &middot;
      <span class="badge badge-border">border</span> above threshold but not in CSV's flag
    </div>
    """)

    # Top 50 section
    parts.append(f"""
    <h2>Top {TOP_N} candidates (sorted by confidence, most foil-like first)</h2>
    <table>
      <thead>
        <tr>
          <th>rank</th><th>scan&nbsp;#</th>
          <th>scan</th><th>reference</th>
          <th>card</th><th>conf</th><th>signals</th><th>verdict</th>
        </tr>
      </thead>
      <tbody>
    """)

    threshold_inserted = False
    for i, r in enumerate(top_rows, start=1):
        # Insert threshold divider once we cross it
        conf = r["_conf"]
        if (not threshold_inserted
                and conf < FOIL_CONFIDENCE_THRESHOLD):
            parts.append(f"""
        <tr><td colspan="8" class="threshold-banner">
          &mdash;&mdash; below threshold ({FOIL_CONFIDENCE_THRESHOLD:+.2f}) &mdash;&mdash;
        </td></tr>
            """)
            threshold_inserted = True

        scan_num = int(r["scan_num"])
        card_id = resolved.get(scan_num)
        is_known = (scan_num == KNOWN_FOIL_SCAN)
        parts.append(_row_html(i, r, card_id, is_known))

    parts.append("</tbody></table>")

    # Bottom 10 sanity section
    parts.append(f"""
    <h2>Bottom {BOTTOM_N} (strongest nonfoil signal — sanity check)</h2>
    <p style="color:#aaa;">These should look like clearly-nonfoil cards:
      dull, flat, matching their reference closely.</p>
    <table>
      <thead>
        <tr>
          <th>rank</th><th>scan&nbsp;#</th>
          <th>scan</th><th>reference</th>
          <th>card</th><th>conf</th><th>signals</th><th>verdict</th>
        </tr>
      </thead>
      <tbody>
    """)

    start_rank = total - BOTTOM_N + 1
    for i, r in enumerate(bottom_rows, start=start_rank):
        scan_num = int(r["scan_num"])
        card_id = resolved.get(scan_num)
        is_known = (scan_num == KNOWN_FOIL_SCAN)
        parts.append(_row_html(i, r, card_id, is_known))

    parts.append("</tbody></table>")

    # Summary / export block with JS to collect radio choices
    parts.append(r"""
    <div class="summary">
      <p>Click below to print your verdicts so you can copy-paste them back:</p>
      <button type="button" onclick="showResults()">Collect verdicts</button>
      <pre id="results"></pre>
    </div>
    <script>
      function showResults() {
        const rows = [];
        document.querySelectorAll('input[type=radio]:checked').forEach(r => {
          const scan = r.name.replace('v', '');
          rows.push(scan + ',' + r.value);
        });
        rows.sort((a, b) => parseInt(a) - parseInt(b));
        const out = document.getElementById('results');
        out.textContent = 'scan_num,verdict\n' + rows.join('\n');
      }
    </script>
    </body></html>
    """)

    html_content = "".join(parts)

    with open(OUT_HTML, "w", encoding="utf-8") as f:
        f.write(html_content)

    print(f"Wrote {OUT_HTML}")
    print(f"\nOpen in browser:\n  {OUT_HTML}")
    print("\nTip: the 'Collect verdicts' button at the bottom "
          "dumps your labels as CSV.")

    # Try to open in default browser
    try:
        webbrowser.open(_file_url(OUT_HTML))
        print("\nOpened in default browser.")
    except Exception as e:
        print(f"\n(Could not auto-open browser: {e})")


if __name__ == "__main__":
    main()
