"""Memory Quality Test orchestrator.

Usage:
    python -m tests.memory_quality.run [--base-url URL] [--model MODEL] [--timeout SECS] [--workers N]

Runs all scenarios against the live API in parallel, collects extraction results,
and writes results.json for downstream LLM verification.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from .api_client import GPTHubTestClient
from .report import generate_results_json
from .scenarios import ALL_SCENARIOS, CATEGORIES

_log = logging.getLogger(__name__)

OUTPUT_DIR = Path(__file__).parent
RESULTS_FILE = OUTPUT_DIR / "results.json"
VERDICTS_DIR = OUTPUT_DIR / "verdicts"


def run_scenario(
    base_url: str,
    docker_container: str | None,
    scenario: dict[str, Any],
    model: str | None,
    timeout: float,
) -> dict[str, Any]:
    """Run a single scenario and return the result dict.

    Each thread gets its own GPTHubTestClient (own httpx connection).
    """
    client = GPTHubTestClient(base_url=base_url, docker_container=docker_container)
    sid = scenario["id"]
    session_id, scope_id = client.unique_ids(sid)

    try:
        # Clean slate
        client.cleanup_scope(scope_id)

        # Snapshot before
        before_files = client.list_memory_files(scope_id)

        # Send the message(s)
        try:
            client.send_message(
                session_id=session_id,
                scope_id=scope_id,
                messages=scenario["messages"],
                model=model,
            )
            api_ok = True
            api_error = None
        except Exception as e:
            _log.error("API error for %s: %s", sid, e)
            api_ok = False
            api_error = str(e)

        # Wait for async extraction
        new_files: list[dict[str, Any]] = []
        if api_ok:
            new_files = client.wait_for_extraction(scope_id, before_files, timeout=timeout)

        # Also read all memories
        all_memories = client.read_scope_memories(scope_id)

        result = {
            "scenario_id": sid,
            "category": scenario["category"],
            "should_save": scenario["should_save"],
            "expected_memory_type": scenario["expected_memory_type"],
            "keywords": scenario["keywords"],
            "messages": scenario["messages"],
            "description": scenario["description"],
            "api_ok": api_ok,
            "api_error": api_error,
            "saved_files": [
                {"filename": f["filename"], "content": f["content"]}
                for f in new_files
            ],
            "all_memories": [
                {"filename": f["filename"], "content": f["content"]}
                for f in all_memories
            ],
            "scope_id": scope_id,
            "session_id": session_id,
        }

        # Quick self-check
        actually_saved = len(new_files) > 0
        if scenario["should_save"] and not actually_saved:
            result["self_check"] = "WARN: expected save but nothing saved"
        elif not scenario["should_save"] and actually_saved:
            result["self_check"] = "WARN: expected no save but files appeared"
        else:
            result["self_check"] = "OK"

        return result
    finally:
        client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Memory Quality Test Runner")
    parser.add_argument("--base-url", default="http://localhost:8000", help="API base URL")
    parser.add_argument("--model", default=None, help="Model to use (default: first available)")
    parser.add_argument("--timeout", type=float, default=150, help="Extraction timeout per scenario (seconds)")
    parser.add_argument("--category", default=None, help="Run only this category")
    parser.add_argument("--scenario", default=None, help="Run only this scenario id")
    parser.add_argument("--workers", "-w", type=int, default=8, help="Parallel workers (default: 8)")
    parser.add_argument("--docker", dest="docker", default="auto",
                        help="Docker container name/id, 'auto' to detect, 'none' for local mode")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    docker_arg = None if args.docker == "none" else args.docker
    probe = GPTHubTestClient(base_url=args.base_url, docker_container=docker_arg)

    # 1. Health check
    if not probe.health_check():
        print(f"ERROR: API at {args.base_url} is not reachable. Is the server running?")
        sys.exit(1)
    mode = f"Docker container {probe._container}" if probe.is_docker else "local filesystem"
    docker_container = probe._container  # resolved container id for workers
    print(f"API at {args.base_url} is healthy (reading files from {mode})")

    # 2. Get model
    model = args.model
    if not model:
        models = probe.list_models()
        if not models:
            print("ERROR: No models available from /v1/models")
            sys.exit(1)
        model = models[0]
    print(f"Using model: {model}")
    probe.close()

    # 3. Select scenarios
    if args.scenario:
        scenarios = [s for s in ALL_SCENARIOS if s["id"] == args.scenario]
        if not scenarios:
            print(f"ERROR: Scenario '{args.scenario}' not found")
            sys.exit(1)
    elif args.category:
        scenarios = CATEGORIES.get(args.category, [])
        if not scenarios:
            print(f"ERROR: Category '{args.category}' not found. Available: {list(CATEGORIES.keys())}")
            sys.exit(1)
    else:
        scenarios = ALL_SCENARIOS

    workers = min(args.workers, len(scenarios))
    print(f"Running {len(scenarios)} scenarios with {workers} parallel workers...")
    t_start = time.monotonic()

    # 4. Run scenarios in parallel
    results: dict[str, dict[str, Any]] = {}
    passed = 0
    warned = 0
    errors = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_to_sid = {
            pool.submit(
                run_scenario, args.base_url, docker_container, s, model, args.timeout
            ): s["id"]
            for s in scenarios
        }
        done_count = 0
        for future in as_completed(future_to_sid):
            sid = future_to_sid[future]
            done_count += 1
            try:
                result = future.result()
                results[sid] = result
                status = result["self_check"]
                elapsed_s = f"{time.monotonic() - t_start:.0f}s"
                if status == "OK":
                    passed += 1
                    print(f"  [{done_count}/{len(scenarios)}] {sid} -> OK  (wall {elapsed_s})")
                else:
                    warned += 1
                    print(f"  [{done_count}/{len(scenarios)}] {sid} -> {status}  (wall {elapsed_s})")
            except Exception as e:
                errors += 1
                print(f"  [{done_count}/{len(scenarios)}] {sid} -> ERROR: {e}")

    # 5. Write results
    VERDICTS_DIR.mkdir(parents=True, exist_ok=True)
    generate_results_json(results, RESULTS_FILE)

    total_time = time.monotonic() - t_start
    total = len(results)
    api_errs = sum(1 for r in results.values() if not r.get("api_ok", True))
    print(f"\n{'='*60}")
    print(f"DONE in {total_time:.0f}s | {total} scenarios | {passed} OK | {warned} warn | {api_errs} api_err | {errors} crash")
    print(f"{'='*60}")
    print(f"Results: {RESULTS_FILE}")


if __name__ == "__main__":
    main()
