from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from reliability_lab.chaos import (
    load_queries,
    run_cache_comparison,
    run_concurrency_comparison,
    run_simulation,
)
from reliability_lab.config import load_config


def write_json(data: dict[str, object], path: str) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def write_comparison_csv(
    sections: list[tuple[str, dict[str, object]]],
    path: str,
) -> None:
    rows = [
        {
            "mode": mode,
            **{
                key: json.dumps(value, ensure_ascii=False)
                if isinstance(value, (dict, list))
                else value
                for key, value in values.items()
            },
        }
        for mode, values in sections
    ]
    fieldnames = list(dict.fromkeys(key for row in rows for key in row))
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def require_mapping(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be a mapping")
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/default.yaml")
    parser.add_argument("--out", default="reports/metrics.json")
    parser.add_argument("--csv-out", default="reports/metrics.csv")
    parser.add_argument(
        "--cache-comparison-out",
        default="reports/cache_comparison.json",
    )
    parser.add_argument(
        "--cache-comparison-csv",
        default="reports/cache_comparison.csv",
    )
    parser.add_argument(
        "--concurrency-comparison-out",
        default="reports/concurrency_comparison.json",
    )
    parser.add_argument(
        "--concurrency-comparison-csv",
        default="reports/concurrency_comparison.csv",
    )
    args = parser.parse_args()
    config = load_config(args.config)
    queries = load_queries()
    metrics = run_simulation(config, queries)
    metrics.write_json(args.out)
    metrics.write_csv(args.csv_out)

    cache_comparison = run_cache_comparison(config, queries)
    write_json(cache_comparison, args.cache_comparison_out)
    write_comparison_csv(
        [
            (
                "without_cache",
                require_mapping(cache_comparison["without_cache"], "without_cache"),
            ),
            ("with_cache", require_mapping(cache_comparison["with_cache"], "with_cache")),
            ("delta", require_mapping(cache_comparison["delta"], "delta")),
        ],
        args.cache_comparison_csv,
    )

    concurrency_comparison = run_concurrency_comparison(config, queries)
    write_json(concurrency_comparison, args.concurrency_comparison_out)
    write_comparison_csv(
        [
            (
                "sequential",
                require_mapping(concurrency_comparison["sequential"], "sequential"),
            ),
            (
                "concurrent",
                require_mapping(concurrency_comparison["concurrent"], "concurrent"),
            ),
            ("summary", {"speedup": concurrency_comparison["speedup"]}),
        ],
        args.concurrency_comparison_csv,
    )

    print(f"wrote {args.out}")
    print(f"wrote {args.csv_out}")
    print(f"wrote {args.cache_comparison_out}")
    print(f"wrote {args.cache_comparison_csv}")
    print(f"wrote {args.concurrency_comparison_out}")
    print(f"wrote {args.concurrency_comparison_csv}")


if __name__ == "__main__":
    main()
