#!/usr/bin/env python3
"""
Aggregate benchmark results from lm-evaluation-harness runs.

Reads JSON results from results/ directory, creates summary tables,
and generates router configuration for model selection.
"""

import json
import os
import sys
from pathlib import Path
from typing import Any

import pandas as pd
from tabulate import tabulate


# Benchmark to task category mapping
BENCHMARK_TO_CATEGORY = {
    "mmlu": "general",
    "gsm8k": "math",
    "hellaswag": "reasoning",
    "humaneval": "coding",
    "ifeval": "instruction_following",
}

# Main metric to extract for each benchmark
BENCHMARK_METRICS = {
    "mmlu": "acc",
    "gsm8k": "exact_match,strict-match",
    "hellaswag": "acc_norm",
    "humaneval": "pass@1",
    "ifeval": "prompt_level_strict_acc",
}

# Fallback model for fast inference
FAST_FALLBACK_MODEL = "llama-3.1-8b-instruct"


def find_results_file(model_dir: Path, benchmark: str) -> Path | None:
    """Find the results JSON file for a benchmark."""
    benchmark_dir = model_dir / benchmark

    if not benchmark_dir.exists():
        return None

    # lm-eval creates files like results_TIMESTAMP.json
    json_files = list(benchmark_dir.glob("results*.json"))
    if not json_files:
        # Also check for results.json directly
        results_file = benchmark_dir / "results.json"
        if results_file.exists():
            return results_file
        return None

    # Return the most recent one
    return max(json_files, key=lambda f: f.stat().st_mtime)


def extract_score(results_data: dict, benchmark: str) -> float | None:
    """Extract the main score from benchmark results."""
    metric_key = BENCHMARK_METRICS.get(benchmark)
    if not metric_key:
        return None

    # Navigate the results structure
    # lm-eval format: results -> task_name -> metric
    results = results_data.get("results", {})

    # Find the task (might have version suffix like mmlu_0)
    task_results = None
    for key in results:
        if key.startswith(benchmark) or benchmark in key.lower():
            task_results = results[key]
            break

    if not task_results:
        # Try direct access
        task_results = results.get(benchmark, {})

    if not task_results:
        return None

    # Try to find the metric
    for metric in metric_key.split(","):
        metric = metric.strip()
        if metric in task_results:
            value = task_results[metric]
            # Handle if value is a dict with 'value' key
            if isinstance(value, dict):
                value = value.get("value", value.get("mean"))
            if value is not None:
                return float(value)

    # Fallback: try common metric names
    for fallback in ["acc", "accuracy", "exact_match", "score"]:
        if fallback in task_results:
            value = task_results[fallback]
            if isinstance(value, dict):
                value = value.get("value", value.get("mean"))
            if value is not None:
                return float(value)

    return None


def load_all_results(results_dir: Path) -> dict[str, dict[str, float]]:
    """Load all benchmark results from the results directory."""
    all_results: dict[str, dict[str, float]] = {}

    if not results_dir.exists():
        print(f"Results directory not found: {results_dir}")
        return all_results

    benchmarks = list(BENCHMARK_METRICS.keys())

    for model_dir in results_dir.iterdir():
        if not model_dir.is_dir():
            continue

        # Skip special directories
        if model_dir.name in ["summary.csv", "router_config.json"]:
            continue

        model_name = model_dir.name
        all_results[model_name] = {}

        for benchmark in benchmarks:
            results_file = find_results_file(model_dir, benchmark)
            if results_file is None:
                continue

            try:
                with open(results_file) as f:
                    data = json.load(f)
                score = extract_score(data, benchmark)
                if score is not None:
                    all_results[model_name][benchmark] = score
            except (json.JSONDecodeError, OSError) as e:
                print(f"Error reading {results_file}: {e}")

    return all_results


def create_summary_table(all_results: dict[str, dict[str, float]]) -> pd.DataFrame:
    """Create a summary DataFrame from results."""
    benchmarks = list(BENCHMARK_METRICS.keys())

    # Create DataFrame
    data = []
    for model, scores in sorted(all_results.items()):
        row = {"model": model}
        for benchmark in benchmarks:
            score = scores.get(benchmark)
            row[benchmark] = score
        data.append(row)

    df = pd.DataFrame(data)
    if not df.empty:
        df = df.set_index("model")

    return df


def find_best_models(df: pd.DataFrame) -> dict[str, str]:
    """Find the best model for each benchmark."""
    best_models = {}

    for benchmark in df.columns:
        column = df[benchmark].dropna()
        if column.empty:
            continue
        best_model = column.idxmax()
        best_models[benchmark] = best_model

    return best_models


def create_router_config(best_models: dict[str, str]) -> dict[str, str]:
    """Create router configuration mapping tasks to best models."""
    router_config = {}

    for benchmark, model in best_models.items():
        category = BENCHMARK_TO_CATEGORY.get(benchmark)
        if category:
            router_config[category] = model

    # Add fast fallback
    router_config["fast_fallback"] = FAST_FALLBACK_MODEL

    return router_config


def format_score(score: float | None) -> str:
    """Format score for display."""
    if score is None:
        return "-"
    return f"{score * 100:.2f}%"


def main():
    """Main entry point."""
    script_dir = Path(__file__).parent
    results_dir = script_dir / "results"

    print("=" * 60)
    print("LLM Benchmark Results Aggregator")
    print("=" * 60)
    print()

    # Load all results
    print(f"Loading results from: {results_dir}")
    all_results = load_all_results(results_dir)

    if not all_results:
        print("No results found!")
        sys.exit(1)

    print(f"Found results for {len(all_results)} models")
    print()

    # Create summary table
    df = create_summary_table(all_results)

    if df.empty:
        print("No valid results to aggregate!")
        sys.exit(1)

    # Format for display
    df_display = df.map(format_score)

    # Print table
    print("=" * 60)
    print("Benchmark Results Summary")
    print("=" * 60)
    print()
    print(tabulate(df_display, headers="keys", tablefmt="grid"))
    print()

    # Find best models
    best_models = find_best_models(df)

    print("=" * 60)
    print("Best Model per Benchmark")
    print("=" * 60)
    for benchmark, model in best_models.items():
        score = df.loc[model, benchmark]
        print(f"  {benchmark}: {model} ({format_score(score)})")
    print()

    # Create router config
    router_config = create_router_config(best_models)

    print("=" * 60)
    print("Router Configuration")
    print("=" * 60)
    print(json.dumps(router_config, indent=2))
    print()

    # Save results
    summary_csv = results_dir / "summary.csv"
    df.to_csv(summary_csv)
    print(f"Saved summary to: {summary_csv}")

    router_config_file = results_dir / "router_config.json"
    with open(router_config_file, "w") as f:
        json.dump(router_config, indent=2, fp=f)
    print(f"Saved router config to: {router_config_file}")

    print()
    print("Done!")


if __name__ == "__main__":
    main()
