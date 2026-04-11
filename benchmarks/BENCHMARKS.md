# LLM Benchmarking Suite

Automated benchmarking of LLM models using lm-evaluation-harness with OpenAI-compatible API.

## Prerequisites

- Python 3.10+
- pip
- bash

## Installation

1. Clone lm-evaluation-harness:

```bash
git clone https://github.com/EleutherAI/lm-evaluation-harness.git
cd lm-evaluation-harness
pip install -e ".[api]"
cd ..
```

2. Install additional dependencies:

```bash
pip install -r requirements.txt
```

## Configuration

### API Settings

Set environment variables or pass as arguments:

```bash
export LLM_BASE_URL="https://api.gpt.mws.ru/v1"
export LLM_API_KEY="your-api-key"
```

### Models

Edit `models.txt` to customize the list of models to benchmark. One model name per line.

## Usage

### Run All Benchmarks

```bash
./run_benchmarks.sh
```

This will:
- Run all 5 benchmarks (mmlu, gsm8k, hellaswag, humaneval, ifeval) on all 15 models
- Skip already completed benchmarks (checks for existing results)
- Save results to `results/{model_name}/{benchmark}/`
- Log progress to `results/benchmark.log`

### Run Specific Model

```bash
./run_benchmarks.sh --model qwen2.5-72b-instruct
```

### Run Specific Benchmark

```bash
./run_benchmarks.sh --benchmark mmlu
```

### Run Single Model + Benchmark

```bash
./run_benchmarks.sh --model qwen2.5-72b-instruct --benchmark mmlu
```

### Custom API Settings

```bash
./run_benchmarks.sh --base-url https://your-api.com/v1 --api-key your-key
```

## Aggregate Results

After running benchmarks, aggregate and analyze results:

```bash
python aggregate_results.py
```

This will:
- Read all JSON results from `results/`
- Print summary table to console
- Save `results/summary.csv` with all scores
- Generate `results/router_config.json` with best model per task category

## Benchmarks

| Benchmark | Category | Few-shot | Max Tokens | Description |
|-----------|----------|----------|------------|-------------|
| mmlu | general | 5 | 512 | Multi-task language understanding |
| gsm8k | math | 5 | 512 | Grade school math problems |
| hellaswag | reasoning | 10 | 512 | Commonsense reasoning |
| humaneval | coding | 0 | 1024 | Code generation |
| ifeval | instruction_following | 0 | 512 | Instruction following |

## Output Files

```
results/
├── {model_name}/
│   ├── mmlu/
│   │   └── results_*.json
│   ├── gsm8k/
│   ├── hellaswag/
│   ├── humaneval/
│   └── ifeval/
├── benchmark.log
├── summary.csv
└── router_config.json
```

### router_config.json Format

```json
{
  "reasoning": "best-model-for-reasoning",
  "coding": "best-model-for-coding",
  "math": "best-model-for-math",
  "general": "best-model-for-general",
  "instruction_following": "best-model-for-instructions",
  "fast_fallback": "llama-3.1-8b-instruct"
}
```

## Retry Logic

The script includes automatic retry on API failures:
- Max 3 attempts per benchmark
- 30 second delay between retries
- Timeout of 120 seconds per request

## Resuming Interrupted Runs

The script automatically skips completed benchmarks. If a run is interrupted, simply run the script again to continue from where it stopped.

## Troubleshooting

### "Model not found" errors

Check that the model name in `models.txt` matches exactly what the API expects.

### Timeout errors

Some benchmarks take longer. Increase timeout in the script if needed:
```bash
# In run_benchmarks.sh, modify:
--model_args "...,timeout=300"
```

### Memory issues

For large models, reduce batch_size in run_benchmarks.sh:
```bash
--batch_size 1
```

## License

MIT
