"""Evaluate semantic-cache thresholds against representative query pairs."""

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from reliability_lab.cache import ResponseCache, _looks_like_false_hit


@dataclass(frozen=True, slots=True)
class EvaluationPair:
    name: str
    cached_query: str
    incoming_query: str
    should_reuse: bool


PAIRS = [
    EvaluationPair("exact", "Explain circuit breaker states", "Explain circuit breaker states", True),
    EvaluationPair(
        "refund_paraphrase",
        "Summarize the refund policy",
        "Summarize refund policy",
        True,
    ),
    EvaluationPair(
        "case_normalization",
        "Explain Circuit Breaker States",
        "explain circuit breaker states",
        True,
    ),
    EvaluationPair(
        "unrelated",
        "Summarize the refund policy",
        "What is the weather today?",
        False,
    ),
    EvaluationPair(
        "refund_year_mismatch",
        "Summarize refund policy for the 2024 deadline",
        "Summarize refund policy for the 2026 deadline",
        False,
    ),
    EvaluationPair(
        "tuition_year_mismatch",
        "What is the tuition fee for the 2024 academic year?",
        "What is the tuition fee for the 2025 academic year?",
        False,
    ),
    EvaluationPair(
        "bullet_count_mismatch",
        "Summarize the admission FAQ in 5 bullets",
        "Summarize the admission FAQ in 3 bullets",
        False,
    ),
]

THRESHOLDS = [0.70, 0.80, 0.85, 0.90, 0.92, 0.95]


def evaluate() -> dict[str, object]:
    pair_results: list[dict[str, str | bool | float]] = []
    for pair in PAIRS:
        pair_results.append(
            {
                **asdict(pair),
                "score": round(
                    ResponseCache.similarity(pair.incoming_query, pair.cached_query),
                    6,
                ),
                "guarded": _looks_like_false_hit(pair.incoming_query, pair.cached_query),
            }
        )

    threshold_results: list[dict[str, object]] = []
    for threshold in THRESHOLDS:
        true_positive = false_positive = true_negative = false_negative = 0
        guarded_false_hits = 0
        for pair_result in pair_results:
            candidate = float(pair_result["score"]) >= threshold
            guarded = bool(pair_result["guarded"])
            accepted = candidate and not guarded
            expected = bool(pair_result["should_reuse"])
            guarded_false_hits += int(candidate and guarded)

            if accepted and expected:
                true_positive += 1
            elif accepted and not expected:
                false_positive += 1
            elif not accepted and expected:
                false_negative += 1
            else:
                true_negative += 1

        precision_denominator = true_positive + false_positive
        recall_denominator = true_positive + false_negative
        threshold_results.append(
            {
                "threshold": threshold,
                "true_positive": true_positive,
                "false_positive": false_positive,
                "true_negative": true_negative,
                "false_negative": false_negative,
                "guarded_false_hits": guarded_false_hits,
                "precision": round(
                    true_positive / precision_denominator if precision_denominator else 0.0,
                    4,
                ),
                "recall": round(
                    true_positive / recall_denominator if recall_denominator else 0.0,
                    4,
                ),
            }
        )

    return {
        "recommended_threshold": 0.90,
        "pairs": pair_results,
        "thresholds": threshold_results,
        "known_limitation": (
            "Lexical n-gram similarity cannot detect non-numeric intent changes; production "
            "systems should add an intent classifier or embedding-based validation."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json-out", default="reports/cache_threshold_analysis.json")
    parser.add_argument("--csv-out", default="reports/cache_threshold_analysis.csv")
    args = parser.parse_args()

    report = evaluate()
    json_path = Path(args.json_out)
    csv_path = Path(args.csv_out)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    rows = report["thresholds"]
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("Threshold evaluation produced no rows")
    with csv_path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(
            csv_file,
            fieldnames=list(rows[0]),
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)

    print(f"wrote {json_path}")
    print(f"wrote {csv_path}")


if __name__ == "__main__":
    main()
