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
            # Original (already-scored) signals
            "dbf": sig["delta_bright_frac"],
            "dms": sig["delta_mean_s"],
            "dhr": sig["delta_hue_range"],
            # Level-2 diagnostic signals (not yet scored)
            "dnc": sig.get("delta_n_bright_clusters", 0.0),
            "dss": sig.get("delta_std_s_bright", 0.0),
            "dle": sig.get("delta_laplacian_energy", 0.0),
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
# X holds the original 3 signals used by the current scoring formula.
# X_all holds all 6 signals (original + 3 Level-2 diagnostics) for
# instrumentation. We DO NOT re-fit the model with the new signals here
# — that's a separate, deliberate step after we evaluate which new
# signals actually separate the classes.
X = np.array([[s["dbf"], s["dms"], s["dhr"]] for s in all_samples])
X_all = np.array([[s["dbf"], s["dms"], s["dhr"],
                   s["dnc"], s["dss"], s["dle"]] for s in all_samples])
y = np.array([s["label"] for s in all_samples])

ALL_FEATURE_NAMES = ("dbf", "dms", "dhr", "dnc", "dss", "dle")
ALL_FEATURE_FULL = (
    "delta_bright_frac",
    "delta_mean_s",
    "delta_hue_range",
    "delta_n_bright_clusters",      # NEW: specular cluster count
    "delta_std_s_bright",            # NEW: bright-pixel saturation variance
    "delta_laplacian_energy",        # NEW: Laplacian energy in bright mask
)

# ---------------------------------------------------------------------------
# Distribution summary

def summarize(name, arr):
    print(f"  {name:<24} n={len(arr):<4} "
          f"min={arr.min():+9.3f}  "
          f"p25={np.percentile(arr, 25):+9.3f}  "
          f"p50={np.percentile(arr, 50):+9.3f}  "
          f"p75={np.percentile(arr, 75):+9.3f}  "
          f"max={arr.max():+9.3f}  "
          f"mean={arr.mean():+9.3f}")

print("\n=== FOIL signals (all 6) ===")
for i, name in enumerate(ALL_FEATURE_NAMES):
    summarize(f"{name} (foil)", X_all[y == 1, i])

print("\n=== NONFOIL signals (all 6) ===")
for i, name in enumerate(ALL_FEATURE_NAMES):
    summarize(f"{name} (nonfoil)", X_all[y == 0, i])

print("\n=== Class difference (foil_mean - nonfoil_mean) ===")
for i, name in enumerate(ALL_FEATURE_NAMES):
    fm = X_all[y == 1, i].mean()
    nm = X_all[y == 0, i].mean()
    fs = X_all[y == 1, i].std() + 1e-9
    ns = X_all[y == 0, i].std() + 1e-9
    pooled_sd = np.sqrt((fs**2 + ns**2) / 2)
    cohens_d = (fm - nm) / pooled_sd
    print(f"  {name}: foil={fm:+9.3f}  nonfoil={nm:+9.3f}  "
          f"diff={fm-nm:+9.3f}  Cohen's_d={cohens_d:+.2f}")

# ---------------------------------------------------------------------------
# Per-signal AUC — answers "if I used ONLY this signal as a classifier,
# how well would it discriminate?". 0.5 = random. >0.7 = useful.
# Computed from rank statistic: AUC = (sum_ranks_pos - n_pos*(n_pos+1)/2)
#                                     / (n_pos * n_neg)
def single_signal_auc(scores, labels):
    """AUC where higher score => more positive."""
    n_pos = int((labels == 1).sum())
    n_neg = int((labels == 0).sum())
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    order = np.argsort(scores, kind="mergesort")
    ranks = np.empty_like(order, dtype=float)
    ranks[order] = np.arange(1, len(scores) + 1)
    pos_rank_sum = ranks[labels == 1].sum()
    return (pos_rank_sum - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)

print("\n=== Per-signal AUC (1.0=perfect, 0.5=random) ===")
print("  Direction-aware: tries both signs and reports the better one,")
print("  so a signal that goes the 'wrong way' still scores fairly.")
auc_table = []
for i, name in enumerate(ALL_FEATURE_NAMES):
    auc_pos = single_signal_auc(X_all[:, i], y)
    auc_neg = single_signal_auc(-X_all[:, i], y)
    best = max(auc_pos, auc_neg)
    sign = "+" if auc_pos >= auc_neg else "-"
    auc_table.append((name, best, sign))

# Sort by AUC for easy reading
auc_table.sort(key=lambda t: t[1], reverse=True)
for name, auc, sign in auc_table:
    full = ALL_FEATURE_FULL[ALL_FEATURE_NAMES.index(name)]
    marker = "  ***" if auc >= 0.70 else ("  ** " if auc >= 0.65 else "  -  ")
    print(f"  {name} (sign {sign})  AUC={auc:.3f}{marker}  {full}")

# ---------------------------------------------------------------------------
# Logistic regression — scipy minimize with L2 regularization and class weights
#
# We fit TWO models for comparison:
#   1) Original 3-feature model (dbf, dms, dhr) — what's currently shipped
#   2) Full 6-feature model (+ dnc, dss, dle) — the Level-2 upgrade candidate
# The threshold sweep is reported for both so we can see exactly how much
# the new signals add (or don't).

n_foil = (y == 1).sum()
n_nonfoil = (y == 0).sum()
class_w = np.where(y == 1, n_nonfoil / n_foil, 1.0)


def neg_log_likelihood(params, X_, y_, w_, reg=0.01):
    b0 = params[0]
    b = params[1:]
    z = b0 + X_ @ b
    # stable log(1 + exp(-z*y_signed)) using logaddexp
    y_signed = 2 * y_ - 1  # 0/1 -> -1/+1
    losses = np.logaddexp(0, -z * y_signed)
    return (w_ * losses).sum() + reg * (b @ b)


def fit_lr(X_in, y_in, w_in, feature_names):
    """Fit standardized logistic regression, return (b0_raw, b_raw, scores)."""
    mu_ = X_in.mean(axis=0)
    sd_ = X_in.std(axis=0) + 1e-9
    Xs_ = (X_in - mu_) / sd_

    x0_ = np.zeros(X_in.shape[1] + 1)
    res = optimize.minimize(neg_log_likelihood, x0_,
                            args=(Xs_, y_in, w_in), method="L-BFGS-B")
    b0_s_ = res.x[0]
    b_s_ = res.x[1:]
    # Convert standardized coefs back to raw-feature space:
    # score = b0_s + sum((X_i - mu_i)/sd_i * b_s_i)
    #       = (b0_s - sum(mu_i/sd_i * b_s_i)) + sum(b_s_i/sd_i * X_i)
    b_raw_ = b_s_ / sd_
    b0_raw_ = b0_s_ - (mu_ / sd_) @ b_s_
    scores_ = b0_raw_ + X_in @ b_raw_
    return b0_raw_, b_raw_, scores_


def threshold_sweep(scores_, y_in, label):
    """Print sweep + return best-F1 (t, F1) and the precision-target rows."""
    print(f"\n=== Threshold sweep — {label} ===")
    print(f"{'thresh':<8} {'TP':<4} {'FP':<4} {'FN':<4} "
          f"{'prec':<7} {'recall':<7} {'F1':<6}")
    best_f1_ = 0.0
    best_t_ = None
    for t in np.arange(-3.0, 3.01, 0.25):
        pred = scores_ >= t
        tp = int((pred & (y_in == 1)).sum())
        fp = int((pred & (y_in == 0)).sum())
        fn = int((~pred & (y_in == 1)).sum())
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
        if f1 > best_f1_:
            best_f1_ = f1
            best_t_ = t
        print(f"{t:+.2f}   {tp:<4} {fp:<4} {fn:<4} "
              f"{prec:<7.2%} {rec:<7.2%} {f1:<6.3f}")
    print(f"\nBest F1 threshold ({label}): "
          f"{best_t_:+.2f} (F1 = {best_f1_:.3f})")

    targets = {}
    for target in (0.95, 0.90, 0.80):
        best_for_prec = None
        for t in np.arange(-3.0, 3.01, 0.05):
            pred = scores_ >= t
            tp = int((pred & (y_in == 1)).sum())
            fp = int((pred & (y_in == 0)).sum())
            fn = int((~pred & (y_in == 1)).sum())
            prec = tp / (tp + fp) if tp + fp else 0.0
            rec = tp / (tp + fn) if tp + fn else 0.0
            if prec >= target and (best_for_prec is None
                                   or rec > best_for_prec[2]):
                best_for_prec = (t, prec, rec, tp, fp)
        targets[target] = best_for_prec
        if best_for_prec:
            t, prec, rec, tp, fp = best_for_prec
            print(f"  {int(target*100)}% precision @ threshold {t:+.2f}: "
                  f"prec={prec:.2%}, recall={rec:.2%} "
                  f"({tp} TP / {fp} FP)")
        else:
            print(f"  {int(target*100)}% precision: UNREACHABLE")
    return best_t_, best_f1_, targets


# --- Fit 1: original 3-feature model ----------------------------------------
print("\n" + "=" * 70)
print("MODEL A: 3-feature (dbf, dms, dhr) — what's currently shipped")
print("=" * 70)
b0_raw, b_raw, scores = fit_lr(X, y, class_w, ALL_FEATURE_NAMES[:3])
print(f"  intercept (bias)       : {b0_raw:+.6f}")
for i, name in enumerate(("dbf", "dms", "dhr")):
    print(f"  W_{name:<4}                : {b_raw[i]:+.6f}")
best_t_3, best_f1_3, targets_3 = threshold_sweep(scores, y, "3-feature")

# --- Fit 2: full 6-feature model --------------------------------------------
print("\n" + "=" * 70)
print("MODEL B: 6-feature (+ dnc, dss, dle) — Level-2 upgrade candidate")
print("=" * 70)
b0_raw_full, b_raw_full, scores_full = fit_lr(X_all, y, class_w,
                                              ALL_FEATURE_NAMES)
print(f"  intercept (bias)       : {b0_raw_full:+.6f}")
for i, name in enumerate(ALL_FEATURE_NAMES):
    flag = "  <-- NEW" if name in ("dnc", "dss", "dle") else ""
    print(f"  W_{name:<4}                : {b_raw_full[i]:+.6f}{flag}")
best_t_6, best_f1_6, targets_6 = threshold_sweep(scores_full, y, "6-feature")

# --- Fit 3: 5-feature model (drop dhr — confirmed noise) --------------------
# In the 6-feature fit, dhr's coefficient is ~10x smaller than dnc/dss
# and ~100x smaller than dms. Confirm we lose nothing by dropping it.
X_5 = np.array([[s["dbf"], s["dms"], s["dnc"], s["dss"], s["dle"]]
                for s in all_samples])
FEATURE_NAMES_5 = ("dbf", "dms", "dnc", "dss", "dle")
print("\n" + "=" * 70)
print("MODEL C: 5-feature (drop dhr) — cleanup of Model B")
print("=" * 70)
b0_raw_5, b_raw_5, scores_5 = fit_lr(X_5, y, class_w, FEATURE_NAMES_5)
print(f"  intercept (bias)       : {b0_raw_5:+.6f}")
for i, name in enumerate(FEATURE_NAMES_5):
    flag = "  <-- NEW" if name in ("dnc", "dss", "dle") else ""
    print(f"  W_{name:<4}                : {b_raw_5[i]:+.6f}{flag}")
best_t_5, best_f1_5, targets_5 = threshold_sweep(scores_5, y, "5-feature")

# --- Side-by-side improvement summary ---------------------------------------
print("\n" + "=" * 70)
print("SIDE-BY-SIDE: 3-feat (shipped) vs 6-feat vs 5-feat (no dhr)")
print("=" * 70)
print(f"  Best F1:  3-feat={best_f1_3:.3f} @ t={best_t_3:+.2f}   "
      f"6-feat={best_f1_6:.3f} @ t={best_t_6:+.2f}   "
      f"5-feat={best_f1_5:.3f} @ t={best_t_5:+.2f}")
for target in (0.95, 0.90, 0.80):
    r3 = targets_3.get(target)
    r6 = targets_6.get(target)
    r5 = targets_5.get(target)
    rec3 = r3[2] if r3 else 0.0
    rec6 = r6[2] if r6 else 0.0
    rec5 = r5[2] if r5 else 0.0
    print(f"  Recall @ {int(target*100)}% precision:  "
          f"3-feat={rec3:.2%}   6-feat={rec6:.2%}   5-feat={rec5:.2%}")

# ---------------------------------------------------------------------------
# Flag possible foils hiding in the 'nonfoil' session 44 data

print("\n=== Top 'nonfoil' candidates per the 6-feature model ===")
print("     (3-feature top candidates have been reviewed in two prior rounds.)")
print("     If any of these are actually foil, relabel + re-run this script.")
nonfoil_full_scores = [(s, scores_full[i])
                       for i, s in enumerate(all_samples) if s["label"] == 0]
nonfoil_full_scores.sort(key=lambda x: x[1], reverse=True)
for s, sc in nonfoil_full_scores[:15]:
    print(f"  s{s['session']:<3} scan={s['scan']:>3}  score={sc:+.3f}  "
          f"{s['name'][:40]:<40}  ({s['set_num']})")

# Also save signal arrays for follow-up analysis. Includes both the
# original 3-feature X (used by the current model) and the full
# 6-feature X_all so downstream scripts can experiment with the new
# signals without re-collecting them.
np.savez(r"D:\Card_Sorter\Scripts\foil_multisignal\tune_data.npz",
         X=X, X_all=X_all, y=y,
         scores=scores, b_raw=b_raw, b0_raw=b0_raw,
         scores_full=scores_full,
         b_raw_full=b_raw_full, b0_raw_full=b0_raw_full,
         feature_names=np.array(ALL_FEATURE_NAMES))
print("\nSaved tune_data.npz (3-feature + 6-feature fits)")
