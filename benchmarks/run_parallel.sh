#!/bin/bash

# Parallel LLM Benchmarking
# Runs all models in parallel

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODELS_FILE="${SCRIPT_DIR}/models.txt"
RESULTS_DIR="${SCRIPT_DIR}/results"
LOGS_DIR="${RESULTS_DIR}/logs"

mkdir -p "${LOGS_DIR}"

# Parse arguments
LIMIT=""
while [ $# -gt 0 ]; do
    case $1 in
        --limit)
            LIMIT="--limit $2"
            shift 2
            ;;
        *)
            shift
            ;;
    esac
done

echo "========================================"
echo "Parallel LLM Benchmarking"
echo "========================================"
echo "Models file: ${MODELS_FILE}"
echo "Results: ${RESULTS_DIR}"
echo "Logs: ${LOGS_DIR}"
[ -n "${LIMIT}" ] && echo "Limit: ${LIMIT}"
echo "========================================"

# Array to store PIDs
declare -a PIDS
declare -a MODELS

# Launch all models in parallel
while IFS= read -r model || [ -n "$model" ]; do
    [ -z "$model" ] && continue

    log_file="${LOGS_DIR}/${model}.log"
    echo "Starting: ${model} → ${log_file}"

    # Run in background, redirect output to log file
    ./run_benchmarks.sh --model "$model" ${LIMIT} > "${log_file}" 2>&1 &

    PIDS+=($!)
    MODELS+=("$model")
done < "${MODELS_FILE}"

echo "========================================"
echo "Launched ${#PIDS[@]} parallel jobs"
echo "========================================"

# Monitor progress
completed=0
failed=0

for i in "${!PIDS[@]}"; do
    pid=${PIDS[$i]}
    model=${MODELS[$i]}

    if wait $pid; then
        echo "✓ Completed: ${model}"
        ((completed++))
    else
        echo "✗ Failed: ${model}"
        ((failed++))
    fi
done

echo "========================================"
echo "All jobs finished"
echo "Completed: ${completed}"
echo "Failed: ${failed}"
echo "========================================"

# Show summary of results
echo ""
echo "Results per model:"
for model in "${MODELS[@]}"; do
    count=$(find "${RESULTS_DIR}/${model}" -name "results*.json" 2>/dev/null | wc -l | tr -d ' ')
    echo "  ${model}: ${count} benchmarks"
done
