#!/usr/bin/env python3
"""
Local benchmark runner using parsed datasets.
Supports: MMLU, HellaSwag, Global MMLU RU, GSM8K, HumanEval, IFEval
"""

import asyncio
import json
import re
import os
import sys
import time
import argparse
from pathlib import Path
from typing import Any
from dataclasses import dataclass, field
import aiohttp
from tqdm.asyncio import tqdm_asyncio

# Configuration
BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.gpt.mws.ru/v1/chat/completions")
API_KEY = os.environ.get("LLM_API_KEY", "sk-ewgiaPC3A6pPDYHwR8siVA")
CONCURRENT_REQUESTS = 10
TIMEOUT = 120


@dataclass
class BenchmarkResult:
    name: str
    total: int = 0
    correct: int = 0
    errors: int = 0
    samples: list = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        if self.total == 0:
            return 0.0
        return self.correct / self.total


async def call_api(session: aiohttp.ClientSession, model: str, messages: list, max_tokens: int = 512) -> str | None:
    """Call chat completions API."""
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0
    }

    try:
        async with session.post(BASE_URL, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data["choices"][0]["message"]["content"]
            else:
                return None
    except Exception as e:
        return None


def parse_mc_answer(response: str) -> str | None:
    """Parse multiple choice answer (A/B/C/D) from response."""
    if not response:
        return None

    # Remove <think>...</think> blocks (DeepSeek R1 format)
    response_clean = re.sub(r"<think>.*?</think>", "", response, flags=re.DOTALL)
    response_clean = response_clean.strip()

    # If nothing left after removing think block, try to find answer in full response
    if not response_clean:
        response_clean = response

    # Try to find answer pattern in cleaned response first
    # "The answer is X" pattern
    match = re.search(r"(?:answer|ответ|выбор)[:\s]*\(?([A-Da-d])\)?", response_clean, re.IGNORECASE)
    if match:
        return match.group(1).upper()

    # Direct answer at start
    response_upper = response_clean.strip().upper()
    if response_upper and response_upper[0] in "ABCD":
        # Check it's not just start of a word
        if len(response_upper) == 1 or not response_upper[1].isalpha():
            return response_upper[0]

    # Look for standalone A/B/C/D
    match = re.search(r"\b([A-D])\b", response_clean, re.IGNORECASE)
    if match:
        return match.group(1).upper()

    # Last resort: check full response for answer patterns
    match = re.search(r"(?:answer|ответ)[:\s]*\(?([A-Da-d])\)?", response, re.IGNORECASE)
    if match:
        return match.group(1).upper()

    # First letter if it's A-D
    if response and response[0] in "ABCD":
        return response[0]

    # Look for standalone A/B/C/D
    match = re.search(r"\b([A-D])\b", response)
    if match:
        return match.group(1)

    return None


def parse_numeric_answer(response: str) -> str | None:
    """Parse numeric answer from GSM8K response."""
    if not response:
        return None

    # Remove <think>...</think> blocks (DeepSeek R1 format)
    response_clean = re.sub(r"<think>.*?</think>", "", response, flags=re.DOTALL)
    response_clean = response_clean.strip()

    if not response_clean:
        response_clean = response

    # Look for #### pattern first (GSM8K format)
    match = re.search(r"####\s*(-?[\d,]+(?:\.\d+)?)", response_clean)
    if match:
        return match.group(1).replace(",", "")

    # Look for "answer is X" pattern
    match = re.search(r"(?:answer|result|total|итого|ответ|equals?)[:\s=]*\$?(-?[\d,]+(?:\.\d+)?)", response_clean, re.IGNORECASE)
    if match:
        return match.group(1).replace(",", "")

    # Find last number in response (after think block removed)
    numbers = re.findall(r"-?[\d,]+(?:\.\d+)?", response_clean)
    if numbers:
        return numbers[-1].replace(",", "")

    return None


async def run_multiple_choice(
    session: aiohttp.ClientSession,
    model: str,
    questions: list,
    benchmark_name: str,
    format_question: callable,
    semaphore: asyncio.Semaphore,
    limit: int | None = None
) -> BenchmarkResult:
    """Run multiple choice benchmark."""
    result = BenchmarkResult(name=benchmark_name)

    if limit:
        questions = questions[:limit]

    async def process_question(idx: int, q: dict):
        async with semaphore:
            prompt = format_question(q)
            messages = [{"role": "user", "content": prompt}]
            response = await call_api(session, model, messages, max_tokens=2048)

            parsed = parse_mc_answer(response)

            # Get correct answer
            if "answer_index" in q:
                correct_idx = q["answer_index"]
                correct = chr(ord("A") + correct_idx)
            elif "answer" in q and isinstance(q["answer"], str):
                correct = q["answer"].upper()
            elif "answer" in q and isinstance(q["answer"], int):
                correct = chr(ord("A") + q["answer"])
            else:
                correct = None

            is_correct = parsed == correct if parsed and correct else False

            return {
                "question": q.get("question", q.get("context", ""))[:100],
                "correct_answer": correct,
                "model_answer": parsed,
                "is_correct": is_correct,
                "raw_response": response[:200] if response else None
            }

    tasks = [process_question(i, q) for i, q in enumerate(questions)]
    samples = await tqdm_asyncio.gather(*tasks, desc=f"Running {benchmark_name}")

    for sample in samples:
        result.total += 1
        if sample["is_correct"]:
            result.correct += 1
        if sample["model_answer"] is None:
            result.errors += 1
        result.samples.append(sample)

    return result


async def run_gsm8k(
    session: aiohttp.ClientSession,
    model: str,
    questions: list,
    semaphore: asyncio.Semaphore,
    limit: int | None = None
) -> BenchmarkResult:
    """Run GSM8K math benchmark."""
    result = BenchmarkResult(name="gsm8k")

    if limit:
        questions = questions[:limit]

    async def process_question(q: dict):
        async with semaphore:
            prompt = f"""Solve this math problem step by step. At the end, write your final numeric answer after "####".

Problem: {q["question"]}

Solution:"""
            messages = [{"role": "user", "content": prompt}]
            response = await call_api(session, model, messages, max_tokens=512)

            parsed = parse_numeric_answer(response)
            correct = q["numeric_answer"].replace(",", "")

            # Compare as floats to handle formatting differences
            try:
                is_correct = abs(float(parsed or 0) - float(correct)) < 0.01
            except:
                is_correct = parsed == correct

            return {
                "question": q["question"][:100],
                "correct_answer": correct,
                "model_answer": parsed,
                "is_correct": is_correct,
                "raw_response": response[:300] if response else None
            }

    tasks = [process_question(q) for q in questions]
    samples = await tqdm_asyncio.gather(*tasks, desc="Running gsm8k")

    for sample in samples:
        result.total += 1
        if sample["is_correct"]:
            result.correct += 1
        if sample["model_answer"] is None:
            result.errors += 1
        result.samples.append(sample)

    return result


async def run_humaneval(
    session: aiohttp.ClientSession,
    model: str,
    tasks_data: list,
    semaphore: asyncio.Semaphore,
    limit: int | None = None
) -> BenchmarkResult:
    """Run HumanEval coding benchmark."""
    result = BenchmarkResult(name="humaneval")

    if limit:
        tasks_data = tasks_data[:limit]

    async def process_task(task: dict):
        async with semaphore:
            prompt = f"""Complete the following Python function. Only output the function body (the code that goes after the function signature), nothing else.

{task["prompt"]}"""
            messages = [{"role": "user", "content": prompt}]
            response = await call_api(session, model, messages, max_tokens=1024)

            if not response:
                return {"task_id": task["task_id"], "is_correct": False, "error": "No response"}

            # Try to execute the code
            try:
                # Combine prompt + response + test
                full_code = task["prompt"] + response + "\n" + task["test"]

                # Execute in isolated namespace
                namespace = {}
                exec(full_code, namespace)

                # Run the check function
                check_fn = namespace.get("check")
                if check_fn:
                    check_fn(namespace[task["entry_point"]])
                    is_correct = True
                else:
                    is_correct = False
            except Exception as e:
                is_correct = False

            return {
                "task_id": task["task_id"],
                "is_correct": is_correct,
                "response": response[:200] if response else None
            }

    tasks = [process_task(t) for t in tasks_data]
    samples = await tqdm_asyncio.gather(*tasks, desc="Running humaneval")

    for sample in samples:
        result.total += 1
        if sample["is_correct"]:
            result.correct += 1
        result.samples.append(sample)

    return result


async def run_ifeval(
    session: aiohttp.ClientSession,
    model: str,
    prompts: list,
    semaphore: asyncio.Semaphore,
    limit: int | None = None
) -> BenchmarkResult:
    """Run IFEval instruction following benchmark."""
    result = BenchmarkResult(name="ifeval")

    if limit:
        prompts = prompts[:limit]

    def check_instruction(response: str, instruction_id: str, kwargs: dict) -> bool:
        """Check if response follows the instruction."""
        if not response:
            return False

        response_lower = response.lower()

        if instruction_id == "punctuation:no_comma":
            return "," not in response

        if instruction_id == "length_constraints:number_words":
            word_count = len(response.split())
            min_words = kwargs.get("min_words", 0)
            max_words = kwargs.get("max_words", float("inf"))
            return min_words <= word_count <= max_words

        if instruction_id == "detectable_format:number_highlighted_sections":
            # Count markdown highlighted sections (*text*)
            highlights = re.findall(r"\*[^*]+\*", response)
            min_sections = kwargs.get("num_highlights", 1)
            return len(highlights) >= min_sections

        if instruction_id == "case:uppercase":
            return response == response.upper()

        if instruction_id == "case:lowercase":
            return response == response.lower()

        if instruction_id == "keywords:include":
            keywords = kwargs.get("keywords", [])
            return all(kw.lower() in response_lower for kw in keywords)

        if instruction_id == "keywords:exclude":
            keywords = kwargs.get("keywords", [])
            return not any(kw.lower() in response_lower for kw in keywords)

        # Default: assume pass for unknown instructions
        return True

    async def process_prompt(item: dict):
        async with semaphore:
            messages = [{"role": "user", "content": item["prompt"]}]
            response = await call_api(session, model, messages, max_tokens=1024)

            # Check all instructions
            instruction_ids = item.get("instruction_id_list", [])
            kwargs_list = item.get("kwargs", [{}] * len(instruction_ids))

            all_passed = True
            instruction_results = []

            for inst_id, kwargs in zip(instruction_ids, kwargs_list):
                passed = check_instruction(response, inst_id, kwargs if kwargs else {})
                instruction_results.append({"instruction": inst_id, "passed": passed})
                if not passed:
                    all_passed = False

            return {
                "prompt": item["prompt"][:100],
                "is_correct": all_passed,
                "instructions": instruction_results,
                "response": response[:200] if response else None
            }

    tasks = [process_prompt(p) for p in prompts]
    samples = await tqdm_asyncio.gather(*tasks, desc="Running ifeval")

    for sample in samples:
        result.total += 1
        if sample["is_correct"]:
            result.correct += 1
        result.samples.append(sample)

    return result


def format_mmlu_question(q: dict) -> str:
    """Format MMLU question."""
    choices = q["choices"]
    letters = ["A", "B", "C", "D"]
    choices_str = "\n".join(f"{l}. {c}" for l, c in zip(letters, choices))
    return f"""Answer the following multiple choice question. Reply with only the letter (A, B, C, or D).

Question: {q["question"]}

{choices_str}

Answer:"""


def format_hellaswag_question(q: dict) -> str:
    """Format HellaSwag question."""
    endings = q["endings"]
    letters = ["A", "B", "C", "D"]
    choices_str = "\n".join(f"{l}. {e}" for l, e in zip(letters, endings))
    return f"""Complete the sentence with the most logical ending. Reply with only the letter (A, B, C, or D).

Context: {q["context"]}

{choices_str}

Answer:"""


async def run_benchmark(model: str, benchmark: str, limit: int | None = None) -> BenchmarkResult:
    """Run a single benchmark."""
    data_dir = Path("parsed_data")

    # Load data
    data_file = data_dir / benchmark / "questions.json"
    if not data_file.exists():
        print(f"Error: {data_file} not found")
        return BenchmarkResult(name=benchmark)

    with open(data_file) as f:
        questions = json.load(f)

    print(f"\nRunning {benchmark} on {model} ({len(questions)} questions, limit={limit or 'all'})")

    semaphore = asyncio.Semaphore(CONCURRENT_REQUESTS)

    async with aiohttp.ClientSession() as session:
        if benchmark in ["mmlu", "global_mmlu_full_ru"]:
            return await run_multiple_choice(
                session, model, questions, benchmark,
                format_mmlu_question, semaphore, limit
            )

        elif benchmark == "hellaswag":
            return await run_multiple_choice(
                session, model, questions, benchmark,
                format_hellaswag_question, semaphore, limit
            )

        elif benchmark == "gsm8k":
            return await run_gsm8k(session, model, questions, semaphore, limit)

        elif benchmark == "humaneval":
            return await run_humaneval(session, model, questions, semaphore, limit)

        elif benchmark == "ifeval":
            return await run_ifeval(session, model, questions, semaphore, limit)

        else:
            print(f"Unknown benchmark: {benchmark}")
            return BenchmarkResult(name=benchmark)


def main():
    parser = argparse.ArgumentParser(description="Run local benchmarks")
    parser.add_argument("--model", required=True, help="Model name")
    parser.add_argument("--benchmark", help="Specific benchmark to run")
    parser.add_argument("--limit", type=int, help="Limit number of questions")
    parser.add_argument("--output", help="Output directory for results")
    args = parser.parse_args()

    benchmarks = ["mmlu", "global_mmlu_full_ru", "hellaswag", "gsm8k", "humaneval", "ifeval"]

    if args.benchmark:
        benchmarks = [args.benchmark]

    output_dir = Path(args.output or f"local_results/{args.model}")
    output_dir.mkdir(parents=True, exist_ok=True)

    results = {}

    for benchmark in benchmarks:
        start_time = time.time()
        result = asyncio.run(run_benchmark(args.model, benchmark, args.limit))
        elapsed = time.time() - start_time

        print(f"\n{benchmark}: {result.correct}/{result.total} = {result.accuracy:.2%} (errors: {result.errors}, time: {elapsed:.1f}s)")

        results[benchmark] = {
            "accuracy": result.accuracy,
            "correct": result.correct,
            "total": result.total,
            "errors": result.errors,
            "time_seconds": elapsed
        }

        # Save detailed results
        with open(output_dir / f"{benchmark}_samples.json", "w") as f:
            json.dump(result.samples, f, ensure_ascii=False, indent=2)

    # Save summary
    with open(output_dir / "summary.json", "w") as f:
        json.dump(results, f, indent=2)

    print(f"\n=== Summary ===")
    for name, r in results.items():
        print(f"{name}: {r['accuracy']:.2%}")

    print(f"\nResults saved to {output_dir}")


if __name__ == "__main__":
    main()
