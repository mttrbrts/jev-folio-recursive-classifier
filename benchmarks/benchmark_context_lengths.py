#!/usr/bin/env python3
"""Compare FOLIO classifications across document-context token budgets."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from folio_classifier import (
    DEFAULT_FOLIO_TREE_CACHE_PATH,
    DEFAULT_MODEL,
    FolioHierarchy,
    TypeSafeClient,
    classify,
    limit_document_context,
)


DEFAULT_BUDGETS = (250, 500, 1000, 2500, 5000, 10000, 0)


def _read_document(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _budget_label(budget: int) -> str:
    return "all" if budget == 0 else f"{budget:,}"


def run_benchmark(
    document_markdown: str,
    hierarchy: FolioHierarchy,
    api_key: str,
    model: str,
    budgets: tuple[int, ...],
    beam_width: int,
    max_depth: int,
    input_cost_per_1k: float | None,
    output_cost_per_1k: float | None,
) -> list[dict[str, Any]]:
    results = []
    for budget in budgets:
        limited_document, document_metadata = limit_document_context(
            document_markdown, budget
        )
        client = TypeSafeClient(
            api_key,
            model=model,
            input_cost_per_1k=input_cost_per_1k,
            output_cost_per_1k=output_cost_per_1k,
        )
        result = classify(
            client,
            hierarchy,
            limited_document,
            beam_width=beam_width,
            max_depth=max_depth,
            document_metadata=document_metadata,
        )
        results.append(
            {
                "budget": budget,
                "budget_label": _budget_label(budget),
                "classification": result["label"],
                "classification_iri": result["iri"],
                "path": result["path"],
                "path_score": result["path_score"],
                "document_context": result["document_context"],
                "api_metadata": result["api_metadata"],
            }
        )
    return results


def write_plot(results: list[dict[str, Any]], output_path: Path) -> None:
    x_values = [
        result["document_context"]["sent_token_estimate"] for result in results
    ]
    y_values = [result["path_score"] for result in results]

    figure, axis = plt.subplots(figsize=(12, 7))
    axis.plot(x_values, y_values, marker="o", linewidth=2)
    axis.set_xlabel("Document context token estimate sent to Jev")
    axis.set_ylabel("FOLIO path score")
    axis.set_title("FOLIO classification by document context length")
    axis.grid(True, alpha=0.3)
    for result, x_value, y_value in zip(results, x_values, y_values):
        axis.annotate(
            f'{result["budget_label"]}: {result["classification"]}',
            (x_value, y_value),
            textcoords="offset points",
            xytext=(8, 8),
            ha="left",
            fontsize=8,
        )
    figure.tight_layout()
    figure.savefig(output_path, dpi=160)
    plt.close(figure)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--document", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, default=Path("context-benchmark.json"))
    parser.add_argument("--output-plot", type=Path, default=Path("context-benchmark.png"))
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--beam-width", type=int, default=3)
    parser.add_argument("--max-depth", type=int, default=5)
    parser.add_argument("--folio-tree-cache", type=Path, default=DEFAULT_FOLIO_TREE_CACHE_PATH)
    parser.add_argument("--input-cost-per-1k", type=float)
    parser.add_argument("--output-cost-per-1k", type=float)
    args = parser.parse_args()

    api_key = os.environ.get("TYPESAFE_API_KEY") or getpass.getpass(
        "TypeSafe API key (hidden): "
    )
    if not api_key:
        parser.error("TYPESAFE_API_KEY is not set and no API key was entered.")

    input_cost = args.input_cost_per_1k
    if input_cost is None and os.environ.get("TYPESAFE_INPUT_COST_PER_1K"):
        input_cost = float(os.environ["TYPESAFE_INPUT_COST_PER_1K"])
    output_cost = args.output_cost_per_1k
    if output_cost is None and os.environ.get("TYPESAFE_OUTPUT_COST_PER_1K"):
        output_cost = float(os.environ["TYPESAFE_OUTPUT_COST_PER_1K"])

    document_markdown = _read_document(args.document)
    hierarchy = FolioHierarchy.from_tree_cache(args.folio_tree_cache.read_bytes())
    results = run_benchmark(
        document_markdown,
        hierarchy,
        api_key,
        args.model,
        DEFAULT_BUDGETS,
        args.beam_width,
        args.max_depth,
        input_cost,
        output_cost,
    )
    args.output_json.write_text(json.dumps(results, indent=2), encoding="utf-8")
    write_plot(results, args.output_plot)
    print(json.dumps(results, indent=2))
    print(f"Wrote {args.output_json} and {args.output_plot}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())