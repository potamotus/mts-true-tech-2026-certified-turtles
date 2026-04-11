#!/bin/bash

# Production LLM Benchmarking
# 1. Pre-caches datasets with single model
# 2. Runs all models in parallel (3 at a time to avoid API overload)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODELS_FILE="${SCRIPT_DIR}/models.txt"
RESULTS_DIR="${SCRIPT_DIR}/results"
LOGS_DIR="${RESULTS_DIR}/logs"

# Models that don't support /completions API (skip loglikelihood benchmarks)
CHAT_ONLY_MODELS="T-pro-it-1.0"

# Max parallel jobs (avoid API overload)
MAX_PARALLEL=3

mkdir -p "${LOGS_DIR}"

echo "========================================"
echo "Production LLM Benchmarking"
echo "========================================"
echo "Models: ${MODELS_FILE}"
echo "Results: ${RESULTS_DIR}"
echo "Max parallel: ${MAX_PARALLEL}"
echo "========================================"

# Step 1: Pre-cache datasets with first model
echo ""
echo "[Step 1/2] Pre-caching datasets..."
first_model=$(head -1 "${MODELS_FILE}")
echo "Using model: ${first_model}"

# Run first model to cache all datasets
log_file="${LOGS_DIR}/${first_model}.log"
echo "Running ${first_model} to cache datasets..."
./run_benchmarks.sh --model "$first_model" > "${log_file}" 2>&1 &
first_pid=$!

# Wait for first model
if wait $first_pid; then
    echo "✓ Datasets cached successfully"
else
    echo "✗ Warning: First model had errors, continuing anyway"
fi

# Step 2: Run remaining models in parallel batches
echo ""
echo "[Step 2/2] Running remaining models (${MAX_PARALLEL} parallel)..."

declare -a PIDS
declare -a MODELS
running=0

while IFS= read -r model || [ -n "$model" ]; do
    [ -z "$model" ] && continue
    [ "$model" = "$first_model" ] && continue  # Skip first model (already done)

    log_file="${LOGS_DIR}/${model}.log"

    # Check if this is a chat-only model
    if echo "$CHAT_ONLY_MODELS" | grep -q "$model"; then
        echo "Starting (chat-only): ${model}"
        # Run only generation benchmarks for chat-only models
        (
            for bench in gsm8k humaneval ifeval; do
                ./run_benchmarks.sh --model "$model" --benchmark "$bench" >> "${log_file}" 2>&1
            done
        ) &
    else
        echo "Starting: ${model}"
        ./run_benchmarks.sh --model "$model" > "${log_file}" 2>&1 &
    fi

    PIDS+=($!)
    MODELS+=("$model")
    ((running++))

    # Wait if we hit max parallel
    if [ $running -ge $MAX_PARALLEL ]; then
        # Wait for any job to finish
        wait -n 2>/dev/null || true
        ((running--))
    fi
done < "${MODELS_FILE}"

# Wait for all remaining jobs
echo ""
echo "Waiting for remaining jobs..."
completed=0
failed=0

for i in "${!PIDS[@]}"; do
    pid=${PIDS[$i]}
    model=${MODELS[$i]}

    if wait $pid 2>/dev/null; then
        echo "✓ Completed: ${model}"
        ((completed++))
    else
        echo "✗ Failed: ${model}"
        ((failed++))
    fi
done

echo "========================================"
echo "Benchmarking Complete"
echo "========================================"
echo "Completed: $((completed + 1))"  # +1 for first model
echo "Failed: ${failed}"
echo ""

# Show results summary
echo "Results per model:"
for d in "${RESULTS_DIR}"/*/; do
    [ -d "$d" ] || continue
    name=$(basename "$d")
    [ "$name" = "logs" ] && continue
    count=$(find "$d" -name "results*.json" 2>/dev/null | wc -l | tr -d ' ')
    echo "  ${name}: ${count} benchmarks"
done

echo ""
echo "Run 'python aggregate_results.py' to generate summary."
