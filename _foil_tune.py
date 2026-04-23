"""Re-fit foil_detect weights using session 51 foils + session 44 nonfoils.

Session 51: 58 scans, all confirmed foil under new lighting (user-labeled).
Session 44: 401 scans under SAME new lighting, user confirmed "same lighting".
    Treated as nonfoil here (foil rate in a casual collection is ~3-5% so label
    noise is small). Will flag extreme-signal session 44 rows at the end so the
    user can check whether any are actually foil (and should be relabeled).

Output:
  - Printed distributions for both classes
  - New weights from logistic regression (scipy.optimize)
  - Threshold sweep: precision/recall at several thresholds
  - Recommended (weights, threshold) pair
"""
import os
import sqlite3

import cv2
import numpy as np
from scipy import optimize

from cards import CARDS_DATA
from foil_detect import detect_foil

# ---------------------------------------------------------------------------

SESSION_FOIL = 51
SESSION_FOIL_DIR = r"D:\Card_Sorter\Scripts\scan_logs\session_20260422_222632\card_crops"

SESSION_NONFOIL = 44
SESSION_NONFOIL_DIR = r"D:\Card_Sorter\Scripts\scan_logs\session_20260421_140152\card_crops"

# Hand-verified foils hiding in session 44 (see foil_candidates_review*.html).
# Relabel these from nonfoil -> foil to clean up the training data.
# Round 1 (15 reviewed): 32 105 44 23 188 25 144 52 82 1 = 10 foils
# Round 2 (25 reviewed): 42 299 = 2 foils
SESSION_44_RELABEL_AS_FOIL = {32, 105, 44, 23, 188, 25, 144, 52, 82, 1,
                              42, 299}
# Round 2 verified-nonfoils: 22, 3, 90, 187, 20, 234, 46, 196, 4, 108, 189,
#   83, 92, 217, 39, 49, 169, 15, 50, 51, 139, 54, 12 (and round 1: 2, 6, 14, 8, 133)

# ---------------------------------------------------------------------------

idx = {}
for c_ in CARDS_DATA:
    key = (c_.get("set", "").lower(), c_.get("collector_number", ""))
    idx.setdefault(key, c_.get("id"))


def collect_signals(session_id: int, crop_dir: str, label: int):
    """Run detect_foil on every crop in the session; return list of dicts."""
    c = sqlite3.connect(r"D:\Card_Sorter\Scripts\collection.db")
    cur = c.cursor()
    cur.execute("""
        SELECT scan_num, name, set_code, collector_number
        FROM scan_history WHERE session_id = ? ORDER BY scan_num
    """, (session_id,))
    rows = cur.fetchall()
    c.close()

    out = []
    for scan_num, name, set_code, num in rows:
        crop = os.path.join(crop_dir, f"card_{scan_num:04d}.jpg")
        if not os.path.isfile(crop):
            continue
        img = cv2.imread(crop)
        if img is None:
            continue
        card_id = idx.get(((set_code or "").lower(), num or ""))
        r = detect_foil(img, card_id=card_id)
        if r["reason"] != "ok":
            continue
        sig = r["signals"]
        out.append({
            "session": session_id,
            "scan": scan_num,
            "name": name,
            "set_num": f"{set_code} #{num}",
            "label": label,
            "dbf": sig["delta_bright_frac"],
            "dms": sig["delta_mean_s"],
            "dhr": sig["delta_hue_range"],
            "current_conf": r["confidence"],
        })
    return out


print("Collecting FOIL signals (session 51)...")
foil = collect_signals(SESSION_FOIL, SESSION_FOIL_DIR, label=1)
print(f"  {len(foil)} scorable foil samples")

print("Collecting session 44 signals (mostly nonfoil, with 10 known-foil relabels)...")
s44 = collect_signals(SESSION_NONFOIL, SESSION_NONFOIL_DIR, label=0)
# Apply relabel set
relabeled = 0
for r in s44:
    if r["scan"] in SESSION_44_RELABEL_AS_FOIL:
        r["label"] = 1
        relabeled += 1
foil_s44 = [r for r in s44 if r["label"] == 1]
nonfoil = [r for r in s44 if r["label"] == 0]
print(f"  {len(s44)} total s44 samples; {relabeled} relabeled as foil")
print(f"  {len(foil_s44)} foils + {len(nonfoil)} nonfoils from s44")

# Merge s44 foils into the foil list so the training set reflects the fix
foil = foil + foil_s44
print(f"Total training set: {len(foil)} foils + {len(nonfoil)} nonfoils")

all_samples = foil + nonfoil
X = np.array([[s["dbf"], s["dms"], s["dhr"]] for s in all_samples])
y = np.array([s["label"] for s in all_samples])

# ---------------------------------------------------------------------------
# Distribution summary

def summarize(name, arr):
    print(f"  {name:<16} n={len(arr):<4} "
          f"min={arr.min():+8.3f}  "
          f"p25={np.percentile(arr, 25):+8.3f}  "
          f"p50={np.percentile(arr, 50):+8.3f}  "
          f"p75={np.percentile(arr, 75):+8.3f}  "
          f"max={arr.max():+8.3f}  "
          f"mean={arr.mean():+8.3f}")

print("\n=== FOIL signals ===")
summarize("dbf (foil)", X[y == 1, 0])
summarize("dms (foil)", X[y == 1, 1])
summarize("dhr (foil)", X[y == 1, 2])

print("\n=== NONFOIL signals ===")
summarize("dbf (nonfoil)", X[y == 0, 0])
summarize("dms (nonfoil)", X[y == 0, 1])
summarize("dhr (nonfoil)", X[y == 0, 2])

print("\n=== Class difference (foil_mean - nonfoil_mean) ===")
for i, name in enumerate(("dbf", "dms", "dhr")):
    fm = X[y == 1, i].mean()
    nm = X[y == 0, i].mean()
    print(f"  {name}: foil={fm:+8.3f}  nonfoil={nm:+8.3f}  diff={fm-nm:+8.3f}")

# ---------------------------------------------------------------------------
# Logistic regression — scipy minimize with L2 regularization and class weights

# Feature scale varies wildly (dbf ~0.1, dms ~50, dhr ~100). Standardize first.
mu = X.mean(axis=0)
sd = X.std(axis=0) + 1e-9
Xs = (X - mu) / sd

# Class weights to counter imbalance
n_foil = (y == 1).sum()
n_nonfoil = (y == 0).sum()
w = np.where(y == 1, n_nonfoil / n_foil, 1.0)

def neg_log_likelihood(params, X, y, w, reg=0.01):
    b0 = params[0]
    b = params[1:]
    z = b0 + X @ b
    # stable log(1 + exp(-z*y_signed)) using logaddexp
    y_signed = 2 * y - 1  # 0/1 -> -1/+1
    losses = np.logaddexp(0, -z * y_signed)
    return (w * losses).sum() + reg * (b @ b)

x0 = np.zeros(4)
result = optimize.minimize(neg_log_likelihood, x0, args=(Xs, y, w),
                           method="L-BFGS-B")
b0_s = result.x[0]
b_s = result.x[1:]

# Convert standardized coefs back to raw-feature space so weights work on
# the original (dbf, dms, dhr) values directly.
# score = b0_s + sum((X_i - mu_i)/sd_i * b_s_i)
#       = (b0_s - sum(mu_i/sd_i * b_s_i)) + sum(b_s_i/sd_i * X_i)
b_raw = b_s / sd
b0_raw = b0_s - (mu / sd) @ b_s

# Compute scores for all samples (these are log-odds, not the old confidence)
scores = b0_raw + X @ b_raw

print("\n=== Fitted logistic regression (raw-feature space) ===")
print(f"  intercept (bias)      : {b0_raw:+.4f}")
print(f"  W_delta_bright_frac   : {b_raw[0]:+.4f}    (was +3.0000)")
print(f"  W_delta_mean_s        : {b_raw[1]:+.6f}    (was -0.0250)")
print(f"  W_delta_hue_range     : {b_raw[2]:+.6f}    (was +0.0050)")

# ---------------------------------------------------------------------------
# Threshold sweep

print("\n=== Threshold sweep on training set ===")
print(f"{'thresh':<8} {'TP':<4} {'FP':<4} {'FN':<4} {'prec':<7} {'recall':<7} {'F1':<6}")
best_f1 = 0.0
best_t = None
for t in np.arange(-3.0, 3.01, 0.25):
    pred = scores >= t
    tp = int((pred & (y == 1)).sum())
    fp = int((pred & (y == 0)).sum())
    fn = int((~pred & (y == 1)).sum())
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    if f1 > best_f1:
        best_f1 = f1
        best_t = t
    print(f"{t:+.2f}   {tp:<4} {fp:<4} {fn:<4} {prec:<7.2%} {rec:<7.2%} {f1:<6.3f}")

print(f"\nBest F1 threshold: {best_t:+.2f} (F1 = {best_f1:.3f})")

# Also: threshold for 95% and 90% precision
for target in (0.95, 0.90, 0.80):
    best_for_prec = None
    for t in np.arange(-3.0, 3.01, 0.05):
        pred = scores >= t
        tp = int((pred & (y == 1)).sum())
        fp = int((pred & (y == 0)).sum())
        fn = int((~pred & (y == 1)).sum())
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        if prec >= target and (best_for_prec is None or rec > best_for_prec[2]):
            best_for_prec = (t, prec, rec, tp, fp)
    if best_for_prec:
        t, prec, rec, tp, fp = best_for_prec
        print(f"  {int(target*100)}% precision @ threshold {t:+.2f}: "
              f"prec={prec:.2%}, recall={rec:.2%} ({tp} TP / {fp} FP)")
    else:
        print(f"  {int(target*100)}% precision: UNREACHABLE")

# ---------------------------------------------------------------------------
# Flag possible foils hiding in the 'nonfoil' session 44 data

print("\n=== Top scoring 'nonfoil' candidates (possibly actually foil) ===")
print("     If any of these are actually foil, relabel + re-run this script.")
nonfoil_scores = [(s, scores[i]) for i, s in enumerate(all_samples) if s["label"] == 0]
nonfoil_scores.sort(key=lambda x: x[1], reverse=True)
for s, sc in nonfoil_scores[:15]:
    print(f"  s{s['session']:<3} scan={s['scan']:>3}  score={sc:+.3f}  "
          f"{s['name'][:40]:<40}  ({s['set_num']})")

# Also save signal arrays for follow-up analysis
np.savez(r"D:\Card_Sorter\Scripts\foil_multisignal\tune_data.npz",
         X=X, y=y, scores=scores, b_raw=b_raw, b0_raw=b0_raw)
print("\nSaved tune_data.npz")
