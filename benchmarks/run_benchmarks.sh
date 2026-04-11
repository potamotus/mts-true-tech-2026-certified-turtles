#!/bin/bash

# LLM Benchmarking Script
# Runs lm-evaluation-harness benchmarks on multiple models via OpenAI-compatible API

set -e

# Configuration
BASE_URL_COMPLETIONS="${LLM_BASE_URL:-https://api.gpt.mws.ru/v1/completions}"
BASE_URL_CHAT="${LLM_BASE_URL_CHAT:-https://api.gpt.mws.ru/v1/chat/completions}"
API_KEY="${LLM_API_KEY:-sk-ewgiaPC3A6pPDYHwR8siVA}"
export OPENAI_API_KEY="${API_KEY}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESULTS_DIR="${SCRIPT_DIR}/results"
MODELS_FILE="${SCRIPT_DIR}/models.txt"
LOG_FILE="${RESULTS_DIR}/benchmark.log"

# Retry configuration
MAX_RETRIES=3
RETRY_DELAY=30
NUM_CONCURRENT=5
LIMIT=""  # Set to number for testing, empty for full run

# Benchmarks: loglikelihood-based (use completions API)
BENCHMARKS_LOGLIK="mmlu hellaswag global_mmlu_full_ru"

# Benchmarks: generation-based (use chat completions API)
BENCHMARKS_GEN="gsm8k humaneval ifeval"

# Get benchmark config: num_fewshot|max_gen_toks|type
get_benchmark_config() {
    case "$1" in
        mmlu)      echo "5|256|loglik" ;;
        hellaswag) echo "10|256|loglik" ;;
        global_mmlu_full_ru) echo "5|256|loglik" ;;
        gsm8k)     echo "5|512|gen" ;;
        humaneval) echo "0|1024|gen" ;;
        ifeval)    echo "0|512|gen" ;;
        *)         echo "0|512|gen" ;;
    esac
}

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Logging function
log() {
    local level=$1
    local message=$2
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    echo -e "${timestamp} [${level}] ${message}" | tee -a "${LOG_FILE}"
}

log_info() {
    log "${BLUE}INFO${NC}" "$1"
}

log_success() {
    log "${GREEN}SUCCESS${NC}" "$1"
}

log_warning() {
    log "${YELLOW}WARNING${NC}" "$1"
}

log_error() {
    log "${RED}ERROR${NC}" "$1"
}

# Check if benchmark is already completed for a model
is_completed() {
    local model=$1
    local benchmark=$2
    local result_dir="${RESULTS_DIR}/${model}/${benchmark}"

    # Check for any results*.json file
    if ls "${result_dir}"/results*.json 1> /dev/null 2>&1; then
        return 0
    fi
    # Also check nested directory (lm-eval quirk)
    if ls "${result_dir}"/"${model}"/results*.json 1> /dev/null 2>&1; then
        return 0
    fi
    return 1
}

# Run a single benchmark with retry logic
run_benchmark() {
    local model=$1
    local benchmark=$2
    local config=$(get_benchmark_config "$benchmark")
    local num_fewshot=$(echo "$config" | cut -d'|' -f1)
    local max_gen_toks=$(echo "$config" | cut -d'|' -f2)
    local bench_type=$(echo "$config" | cut -d'|' -f3)
    local output_dir="${RESULTS_DIR}/${model}/${benchmark}"

    mkdir -p "${output_dir}"
    mkdir -p "${RESULTS_DIR}"

    # Select model type and base URL based on benchmark type
    local model_type
    local base_url
    local extra_args=""

    if [ "$bench_type" = "loglik" ]; then
        model_type="local-completions"
        base_url="${BASE_URL_COMPLETIONS}"
    else
        model_type="local-chat-completions"
        base_url="${BASE_URL_CHAT}"
        extra_args="--apply_chat_template"
    fi

    # Build limit argument if set
    local limit_arg=""
    if [ -n "${LIMIT}" ]; then
        limit_arg="--limit ${LIMIT}"
    fi

    local attempt=1
    while [ $attempt -le $MAX_RETRIES ]; do
        log_info "Running ${benchmark} on ${model} (attempt ${attempt}/${MAX_RETRIES})"
        log_info "Type: ${model_type}, num_fewshot=${num_fewshot}, max_gen_toks=${max_gen_toks}"

        local start_time=$(date +%s)

        if lm_eval \
            --model "${model_type}" \
            --model_args "model=${model},base_url=${base_url},num_concurrent=${NUM_CONCURRENT},max_retries=3,timeout=120,tokenized_requests=False,max_length=8192,tokenizer=gpt2" \
            --tasks "${benchmark}" \
            --num_fewshot "${num_fewshot}" \
            --batch_size 1 \
            --output_path "${output_dir}" \
            --log_samples \
            ${extra_args} \
            --gen_kwargs "max_gen_toks=${max_gen_toks}" \
            ${limit_arg} \
            2>&1 | tee -a "${LOG_FILE}"; then

            local end_time=$(date +%s)
            local duration=$((end_time - start_time))
            log_success "${benchmark} on ${model} completed in ${duration}s"
            return 0
        else
            local exit_code=$?
            log_error "Attempt ${attempt} failed with exit code ${exit_code}"

            if [ $attempt -lt $MAX_RETRIES ]; then
                log_warning "Retrying in ${RETRY_DELAY} seconds..."
                sleep $RETRY_DELAY
            fi
        fi

        attempt=$((attempt + 1))
    done

    log_error "All ${MAX_RETRIES} attempts failed for ${benchmark} on ${model}"
    return 1
}

# Main execution
main() {
    log_info "=========================================="
    log_info "LLM Benchmarking Script Started"
    log_info "=========================================="
    log_info "Completions URL: ${BASE_URL_COMPLETIONS}"
    log_info "Chat URL: ${BASE_URL_CHAT}"
    log_info "Results directory: ${RESULTS_DIR}"

    mkdir -p "${RESULTS_DIR}"

    # Check if models file exists
    if [ ! -f "${MODELS_FILE}" ]; then
        log_error "Models file not found: ${MODELS_FILE}"
        exit 1
    fi

    # All benchmarks
    local all_benchmarks="${BENCHMARKS_LOGLIK} ${BENCHMARKS_GEN}"

    # Count models and benchmarks
    local total_models=$(grep -c . "${MODELS_FILE}" || echo 0)
    local total_benchmarks=$(echo $all_benchmarks | wc -w | tr -d ' ')
    local total_tasks=$((total_models * total_benchmarks))

    log_info "Models to benchmark: ${total_models}"
    log_info "Benchmarks per model: ${total_benchmarks} (${all_benchmarks})"
    log_info "Total tasks: ${total_tasks}"
    log_info "------------------------------------------"

    local completed=0
    local skipped=0
    local failed=0
    local model_index=0

    while IFS= read -r model || [ -n "$model" ]; do
        # Skip empty lines
        [ -z "$model" ] && continue

        model_index=$((model_index + 1))

        log_info "=========================================="
        log_info "Model ${model_index}/${total_models}: ${model}"
        log_info "=========================================="

        local benchmark_index=0
        for benchmark in $all_benchmarks; do
            benchmark_index=$((benchmark_index + 1))

            log_info "Benchmark ${benchmark_index}/${total_benchmarks}: ${benchmark}"

            # Check if already completed
            if is_completed "$model" "$benchmark"; then
                log_warning "Skipping ${benchmark} on ${model} (already completed)"
                skipped=$((skipped + 1))
                continue
            fi

            # Run benchmark
            if run_benchmark "$model" "$benchmark"; then
                completed=$((completed + 1))
            else
                failed=$((failed + 1))
            fi

            log_info "------------------------------------------"
        done
    done < "${MODELS_FILE}"

    log_info "=========================================="
    log_info "Benchmarking Complete"
    log_info "=========================================="
    log_info "Completed: ${completed}"
    log_info "Skipped (already done): ${skipped}"
    log_info "Failed: ${failed}"

    if [ $failed -gt 0 ]; then
        log_warning "Some benchmarks failed. Check ${LOG_FILE} for details."
        exit 1
    fi

    log_success "All benchmarks completed successfully!"
}

# Parse command line arguments
while [ $# -gt 0 ]; do
    case $1 in
        --base-url)
            BASE_URL_COMPLETIONS="$2/completions"
            BASE_URL_CHAT="$2/chat/completions"
            shift 2
            ;;
        --api-key)
            API_KEY="$2"
            export OPENAI_API_KEY="${API_KEY}"
            shift 2
            ;;
        --model)
            SINGLE_MODEL="$2"
            shift 2
            ;;
        --benchmark)
            SINGLE_BENCHMARK="$2"
            shift 2
            ;;
        --limit)
            LIMIT="$2"
            shift 2
            ;;
        --concurrent)
            NUM_CONCURRENT="$2"
            shift 2
            ;;
        --help)
            echo "Usage: $0 [options]"
            echo ""
            echo "Options:"
            echo "  --base-url URL     OpenAI-compatible API base URL (without /completions)"
            echo "  --api-key KEY      API key for authentication"
            echo "  --model MODEL      Run only for a specific model"
            echo "  --benchmark BENCH  Run only a specific benchmark"
            echo "  --limit N          Limit number of examples per benchmark (for testing)"
            echo "  --concurrent N     Number of concurrent API requests (default: 5)"
            echo "  --help             Show this help message"
            echo ""
            echo "Benchmarks:"
            echo "  Loglikelihood (completions API): mmlu, hellaswag"
            echo "  Generation (chat API): gsm8k, humaneval, ifeval"
            echo ""
            echo "Environment variables:"
            echo "  LLM_BASE_URL       Alternative to --base-url"
            echo "  LLM_API_KEY        Alternative to --api-key"
            exit 0
            ;;
        *)
            log_error "Unknown option: $1"
            exit 1
            ;;
    esac
done

# Run single model/benchmark if specified
if [ -n "${SINGLE_MODEL}" ] && [ -n "${SINGLE_BENCHMARK}" ]; then
    mkdir -p "${RESULTS_DIR}"
    run_benchmark "${SINGLE_MODEL}" "${SINGLE_BENCHMARK}"
elif [ -n "${SINGLE_MODEL}" ]; then
    mkdir -p "${RESULTS_DIR}"
    all_benchmarks="${BENCHMARKS_LOGLIK} ${BENCHMARKS_GEN}"
    for benchmark in $all_benchmarks; do
        run_benchmark "${SINGLE_MODEL}" "$benchmark"
    done
elif [ -n "${SINGLE_BENCHMARK}" ]; then
    mkdir -p "${RESULTS_DIR}"
    while IFS= read -r model || [ -n "$model" ]; do
        [ -z "$model" ] && continue
        run_benchmark "$model" "${SINGLE_BENCHMARK}"
    done < "${MODELS_FILE}"
else
    main
fi
