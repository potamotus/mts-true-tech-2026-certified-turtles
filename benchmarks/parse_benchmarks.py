#!/usr/bin/env python3
"""Parse benchmark datasets and extract questions with correct answers."""

import json
import os
from pathlib import Path

def parse_global_mmlu_ru():
    """Parse Global MMLU Russian dataset from HuggingFace."""
    from datasets import load_dataset

    output_dir = Path("parsed_data/global_mmlu_full_ru")
    output_dir.mkdir(parents=True, exist_ok=True)

    print("Loading global_mmlu_full_ru...")
    ds = load_dataset("CohereForAI/Global-MMLU", "ru", split="test")

    data = []
    for item in ds:
        data.append({
            "question": item["question"],
            "choices": [item["option_a"], item["option_b"], item["option_c"], item["option_d"]],
            "answer": item["answer"],  # A, B, C, or D
            "answer_index": ord(item["answer"]) - ord("A"),
            "subject": item.get("subject", "unknown")
        })

    output_file = output_dir / "questions.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"Saved {len(data)} questions to {output_file}")
    return data

def parse_gsm8k():
    """Parse GSM8K math dataset."""
    from datasets import load_dataset

    output_dir = Path("parsed_data/gsm8k")
    output_dir.mkdir(parents=True, exist_ok=True)

    print("Loading gsm8k...")
    ds = load_dataset("openai/gsm8k", "main", split="test")

    data = []
    for item in ds:
        # Extract numeric answer from the answer string
        answer_text = item["answer"]
        # GSM8K format: "... #### <number>"
        if "####" in answer_text:
            numeric_answer = answer_text.split("####")[-1].strip()
        else:
            numeric_answer = answer_text

        data.append({
            "question": item["question"],
            "answer": answer_text,
            "numeric_answer": numeric_answer
        })

    output_file = output_dir / "questions.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"Saved {len(data)} questions to {output_file}")
    return data

def parse_humaneval():
    """Parse HumanEval coding dataset."""
    from datasets import load_dataset

    output_dir = Path("parsed_data/humaneval")
    output_dir.mkdir(parents=True, exist_ok=True)

    print("Loading humaneval...")
    ds = load_dataset("openai/openai_humaneval", split="test")

    data = []
    for item in ds:
        data.append({
            "task_id": item["task_id"],
            "prompt": item["prompt"],
            "canonical_solution": item["canonical_solution"],
            "test": item["test"],
            "entry_point": item["entry_point"]
        })

    output_file = output_dir / "questions.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"Saved {len(data)} tasks to {output_file}")
    return data

def parse_ifeval():
    """Parse IFEval instruction following dataset."""
    from datasets import load_dataset

    output_dir = Path("parsed_data/ifeval")
    output_dir.mkdir(parents=True, exist_ok=True)

    print("Loading ifeval...")
    ds = load_dataset("google/IFEval", split="train")

    data = []
    for item in ds:
        data.append({
            "prompt": item["prompt"],
            "instruction_id_list": item.get("instruction_id_list", []),
            "kwargs": item.get("kwargs", [])
        })

    output_file = output_dir / "questions.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"Saved {len(data)} prompts to {output_file}")
    return data

def main():
    print("Parsing benchmark datasets...\n")

    try:
        parse_global_mmlu_ru()
    except Exception as e:
        print(f"Error parsing global_mmlu_full_ru: {e}")

    print()

    try:
        parse_gsm8k()
    except Exception as e:
        print(f"Error parsing gsm8k: {e}")

    print()

    try:
        parse_humaneval()
    except Exception as e:
        print(f"Error parsing humaneval: {e}")

    print()

    try:
        parse_ifeval()
    except Exception as e:
        print(f"Error parsing ifeval: {e}")

    print("\nDone!")

if __name__ == "__main__":
    main()
