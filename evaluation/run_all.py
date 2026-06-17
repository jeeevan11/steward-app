#!/usr/bin/env python3
"""evaluation/run_all.py — CLI entry for the evaluation framework (Phase 4).

Runs every dataset under evaluation/datasets/ through the REAL brain (in dry-run on
a fresh in-memory DB), prints a human-readable report, writes a timestamped JSON
report under evaluation/reports/, and compares the aggregate metrics against the
previous report to flag regressions (any accuracy that dropped, or any error-rate
that rose).

Usage:
    python evaluation/run_all.py --no-llm          # deterministic, no API key (CI)
    python evaluation/run_all.py                   # real LLM (needs OPENROUTER_API_KEY)
    python evaluation/run_all.py --no-llm --stamp my-run
    python evaluation/run_all.py --no-llm --no-write   # don't persist a report

Exit code is 0 on a clean run. With --strict it is 1 if any regression is detected,
so CI can gate on it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

# Make the repo root importable when run as a bare script (python evaluation/run_all.py).
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from evaluation import metrics as metrics_mod  # noqa: E402
from evaluation import runner  # noqa: E402

DATASETS_DIR = os.path.join(_HERE, "datasets")
REPORTS_DIR = os.path.join(_HERE, "reports")
HR = "=" * 64


# ─────────────────────────────────────────────────────────────────────────────
# Discovery + stamping
# ─────────────────────────────────────────────────────────────────────────────
def discover_datasets(datasets_dir: str = DATASETS_DIR) -> list[str]:
    if not os.path.isdir(datasets_dir):
        return []
    names = sorted(f for f in os.listdir(datasets_dir) if f.endswith(".jsonl"))
    return [os.path.join(datasets_dir, n) for n in names]


def make_stamp(explicit: str = "") -> str:
    """A filesystem-safe stamp. Prefer an explicit one (reproducible reports/tests);
    otherwise derive from local time. No Date.now()/locale surprises — plain strftime."""
    if explicit:
        return "".join(c if (c.isalnum() or c in "-_.") else "-" for c in explicit)
    return time.strftime("%Y%m%dT%H%M%S", time.localtime())


# ─────────────────────────────────────────────────────────────────────────────
# Report I/O
# ─────────────────────────────────────────────────────────────────────────────
def write_report(bundle: dict, stamp: str, reports_dir: str = REPORTS_DIR) -> str:
    os.makedirs(reports_dir, exist_ok=True)
    path = os.path.join(reports_dir, f"{stamp}.json")
    # Never clobber an existing report (two runs in the same second, or a reused
    # --stamp): append a short suffix so the prior report survives for the regression
    # compare. Sortable suffix keeps "most recent = last by filename" true.
    if os.path.exists(path):
        suffix = 1
        while os.path.exists(os.path.join(reports_dir, f"{stamp}-{suffix:03d}.json")):
            suffix += 1
        stamp = f"{stamp}-{suffix:03d}"
        path = os.path.join(reports_dir, f"{stamp}.json")
    payload = dict(bundle)
    payload["stamp"] = stamp
    payload["created_at"] = int(time.time())
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
    return path


def previous_report(reports_dir: str = REPORTS_DIR, *, exclude: str = "") -> dict | None:
    """The most recent report on disk (by filename, which is time-sortable), excluding
    the one we just wrote. None if there is no prior report."""
    if not os.path.isdir(reports_dir):
        return None
    files = sorted(f for f in os.listdir(reports_dir)
                   if f.endswith(".json") and f != os.path.basename(exclude))
    if not files:
        return None
    try:
        with open(os.path.join(reports_dir, files[-1]), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001 - a corrupt prior report should not break a run
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Regression compare
# ─────────────────────────────────────────────────────────────────────────────
def compare_regressions(current: dict, previous: dict | None, *, eps: float = 1e-9) -> list[dict]:
    """Return a list of regression records by comparing flat aggregate metrics.

    A regression is: a HIGHER_IS_BETTER metric that dropped, OR a LOWER_IS_BETTER
    metric that rose, OR the error count rising. Each record carries the metric name,
    previous value, current value, and delta."""
    if not previous:
        return []
    cur = current.get("metrics", {})
    prev = previous.get("metrics", {})
    regressions: list[dict] = []

    for key in metrics_mod.HIGHER_IS_BETTER:
        if key in cur and key in prev and cur[key] + eps < prev[key]:
            regressions.append({"metric": key, "prev": prev[key], "curr": cur[key],
                                "delta": round(cur[key] - prev[key], 4), "direction": "dropped"})
    for key in metrics_mod.LOWER_IS_BETTER:
        if key in cur and key in prev and cur[key] > prev[key] + eps:
            regressions.append({"metric": key, "prev": prev[key], "curr": cur[key],
                                "delta": round(cur[key] - prev[key], 4), "direction": "rose"})
    if int(cur.get("n_errors", 0)) > int(prev.get("n_errors", 0)):
        regressions.append({"metric": "n_errors", "prev": prev.get("n_errors", 0),
                            "curr": cur.get("n_errors", 0),
                            "delta": cur.get("n_errors", 0) - prev.get("n_errors", 0),
                            "direction": "rose"})
    return regressions


# ─────────────────────────────────────────────────────────────────────────────
# Printing
# ─────────────────────────────────────────────────────────────────────────────
def _fmt(v) -> str:
    return f"{v:.3f}" if isinstance(v, float) else str(v)


def print_report(bundle: dict, regressions: list[dict], *, prev_stamp: str | None) -> None:
    print(HR)
    mode = "STRUCTURE ONLY (--no-llm)" if bundle.get("no_llm") else "REAL LLM"
    print(f"EVALUATION REPORT  —  {mode}")
    print(f"scenarios: {bundle.get('n_scenarios', 0)}  ·  datasets: {len(bundle.get('datasets', {}))}")
    print(HR)

    for name, d in bundle.get("datasets", {}).items():
        m = d["metrics"]
        print(f"\n[{name}]  (n={m['n_total']}, errors={m['n_errors']})")
        print(f"   tier_accuracy        {_fmt(m['tier_accuracy'])}")
        print(f"   suppression_accuracy {_fmt(m['suppression_accuracy'])}")
        print(f"   escalation_accuracy  {_fmt(m['escalation_accuracy'])}")
        print(f"   false_positive_rate  {_fmt(m['false_positive_rate'])}")
        print(f"   false_negative_rate  {_fmt(m['false_negative_rate'])}")
        # per-scenario misses, so a regression is debuggable from the console alone
        for r in d["records"]:
            if r.get("error"):
                print(f"      ! {r['id']}: ERROR {r['error']}")
            elif int(r.get("actual_tier", -1)) != int(r.get("expected_tier", -2)):
                print(f"      - {r['id']}: tier {r.get('actual_tier')} (expected {r['expected_tier']})"
                      f"  [cat {r.get('actual_category')}]")

    agg = bundle.get("metrics", {})
    print(f"\n{HR}\nAGGREGATE")
    for key in ("tier_accuracy", "suppression_accuracy", "escalation_accuracy",
                "category_accuracy", "false_positive_rate", "false_negative_rate"):
        print(f"   {key:<22}{_fmt(agg.get(key))}")
    da = agg.get("draft_acceptance", {})
    if da.get("available"):
        print(f"   draft_acceptance       {_fmt(da.get('acceptance'))} "
              f"(approve={da['approve']} edit={da['edit']} skip={da['skip']})")
    else:
        print("   draft_acceptance       n/a (no learning_events; hook ready for live DB)")

    print(f"\n{HR}\nREGRESSION CHECK")
    if prev_stamp is None:
        print("   no previous report to compare against (baseline run).")
    elif not regressions:
        print(f"   no regressions vs previous report ({prev_stamp}).")
    else:
        print(f"   {len(regressions)} REGRESSION(S) vs previous report ({prev_stamp}):")
        for r in regressions:
            print(f"      ! {r['metric']}: {r['direction']} {_fmt(r['prev'])} -> {_fmt(r['curr'])} "
                  f"(delta {_fmt(r['delta'])})")
    print(HR)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Run the evaluation benchmark suite.")
    ap.add_argument("--no-llm", action="store_true",
                    help="deterministic FakeLLM mode; no API key / network needed")
    ap.add_argument("--stamp", default="", help="explicit report stamp (else derived from local time)")
    ap.add_argument("--no-write", action="store_true", help="do not persist a JSON report")
    ap.add_argument("--strict", action="store_true", help="exit non-zero if a regression is detected")
    ap.add_argument("--datasets-dir", default=DATASETS_DIR, help="override the datasets directory")
    args = ap.parse_args(argv)

    paths = discover_datasets(args.datasets_dir)
    if not paths:
        print(f"No datasets (*.jsonl) found under {args.datasets_dir}", file=sys.stderr)
        return 2

    bundle = runner.run_paths(paths, no_llm=args.no_llm)

    written = ""
    prev = previous_report()
    if not args.no_write:
        stamp = make_stamp(args.stamp)
        written = write_report(bundle, stamp)
        # re-read the previous excluding the one we just wrote
        prev = previous_report(exclude=written)

    regressions = compare_regressions(bundle, prev)
    prev_stamp = prev.get("stamp") if prev else None
    print_report(bundle, regressions, prev_stamp=prev_stamp)
    if written:
        print(f"report written: {written}")

    if args.strict and regressions:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
