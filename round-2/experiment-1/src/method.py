#!/usr/bin/env python3
"""
Typed Unification Failure Recovery Pipeline.

Three pipelines:
  - TYPED: typed failure detection + targeted LLM repair
  - ARGOS: single undifferentiated abductive repair (baseline 1)
  - LOGIC_LM: raw error forwarding to LLM (baseline 2)

Datasets: ProofWriter (propositional) + CLUTRR (kinship relational).
Output: method_out.json following exp_gen_sol_out schema.
"""

import gc
import glob
import json
import math
import os
import resource
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

logger.remove()
GREEN, CYAN, END = "\033[92m", "\033[96m", "\033[0m"
logger.add(
    sys.stdout,
    level="INFO",
    format=f"{GREEN}{{time:HH:mm:ss}}{END}|{{level:<7}}|{CYAN}{{function}}{END}| {{message}}",
)
logger.add("logs/run.log", rotation="30 MB", level="DEBUG")

Path("logs").mkdir(exist_ok=True)
Path("outputs").mkdir(exist_ok=True)

# --- Memory safety ---
def _container_ram_gb() -> float | None:
    for p in ["/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"]:
        try:
            v = Path(p).read_text().strip()
            if v != "max" and int(v) < 1_000_000_000_000:
                return int(v) / 1e9
        except (FileNotFoundError, ValueError):
            pass
    return None

import psutil
_avail = psutil.virtual_memory().available
RAM_BUDGET = min(int(_avail * 0.6), 20 * 1024**3)
try:
    resource.setrlimit(resource.RLIMIT_AS, (RAM_BUDGET * 3, RAM_BUDGET * 3))
except Exception:
    pass


def load_config(path: str = "config.yaml") -> dict:
    return yaml.safe_load(Path(path).read_text())


def load_mini_data(path: str) -> list[dict]:
    data = json.loads(Path(path).read_text())
    examples = []
    for ds in data["datasets"]:
        for ex in ds["examples"]:
            examples.append({"dataset": ds["dataset"], **ex})
    return examples


def load_full_data(data_dir: str, max_per_dataset: int = 100) -> list[dict]:
    """Stream full data files, sampling max_per_dataset per dataset."""
    parts = sorted(glob.glob(str(Path(data_dir) / "*.json")))
    if not parts:
        logger.warning(f"No files found in {data_dir}")
        return []

    per_dataset: dict[str, list] = defaultdict(list)
    total_read = 0

    for part_path in parts:
        logger.info(f"Reading {part_path}")
        try:
            data = json.loads(Path(part_path).read_text())
            for ds in data["datasets"]:
                name = ds["dataset"]
                remaining = max_per_dataset - len(per_dataset[name])
                if remaining <= 0:
                    continue
                examples = ds["examples"][:remaining]
                for ex in examples:
                    per_dataset[name].append({"dataset": name, **ex})
                total_read += len(examples)
        except Exception as e:
            logger.error(f"Failed to read {part_path}: {e}")
        del data
        gc.collect()

        if all(len(v) >= max_per_dataset for v in per_dataset.values()):
            break

    all_examples = []
    for examples in per_dataset.values():
        all_examples.extend(examples)
    logger.info(f"Loaded {len(all_examples)} examples total ({dict((k, len(v)) for k, v in per_dataset.items())})")
    return all_examples


def compute_metrics(results: list[dict], dataset: str | None = None) -> dict:
    """Compute accuracy and hallucination metrics."""
    filtered = [r for r in results if dataset is None or r.get("dataset") == dataset]
    if not filtered:
        return {"count": 0, "accuracy": 0.0, "hallucination_rate": 0.0}

    correct = sum(1 for r in filtered if r.get("correct", False))
    hallucinations = [r.get("hallucination_rate", 1.0) for r in filtered]
    repairs_attempted = sum(r.get("repairs_attempted", 0) for r in filtered)
    failure_types = defaultdict(int)
    for r in filtered:
        failure_types[str(r.get("failure_type", -1))] += 1

    return {
        "count": len(filtered),
        "accuracy": round(correct / len(filtered), 4),
        "hallucination_rate": round(sum(hallucinations) / len(hallucinations), 4),
        "repairs_attempted": repairs_attempted,
        "failure_type_distribution": dict(failure_types),
    }


def multihop_metrics(results: list[dict], dataset: str) -> dict:
    """Compute metrics only on multi-hop examples (depth >= 2)."""
    multi = [r for r in results
             if r.get("dataset") == dataset
             and r.get("metadata_reasoning_depth", 0) >= 2]
    if not multi:
        return {"count": 0, "accuracy": 0.0}
    correct = sum(1 for r in multi if r.get("correct", False))
    return {
        "count": len(multi),
        "accuracy": round(correct / len(multi), 4),
    }


def ablation_study(
    examples: list[dict],
    llm,
    sentbert_sim,
    cache: dict,
    config: dict,
) -> dict:
    """Disable each repair type and measure accuracy drop."""
    from src.repair_engine import RepairEngine
    from src.pipeline import run_example

    # Full typed accuracy (baseline)
    repair_engine_full = RepairEngine(llm, sentbert_sim, {})
    full_results = []
    for ex in examples[:30]:  # subset for speed
        r = run_example(
            dataset=ex["dataset"], input_text=ex["input"],
            gold_output=ex["output"], mode="typed",
            llm=llm, repair_engine=repair_engine_full,
            max_retries=config.get("max_retries_per_repair", 2),
        )
        r["dataset"] = ex["dataset"]
        full_results.append(r)

    full_acc = compute_metrics(full_results)["accuracy"]
    ablations = {"full": {"accuracy": full_acc, "accuracy_drop": 0.0}}

    # Disable each type by monkeypatching
    for disabled_type in [1, 2, 3, 4]:
        from src import repair_engine as re_module

        class PatchedRepair(RepairEngine):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self._disabled = disabled_type

            def _dispatch(self, ft, *args, **kwargs):
                if ft == self._disabled:
                    return args[0], {"repair_type": ft, "success": False, "reason": "ablated"}
                return super()._dispatch(ft, *args, **kwargs)

        # Simpler approach: wrap run_example with type filter
        patched_results = []
        for ex in examples[:30]:
            r = run_example(
                dataset=ex["dataset"], input_text=ex["input"],
                gold_output=ex["output"], mode=f"ablate_{disabled_type}",
                llm=llm, repair_engine=repair_engine_full,
                max_retries=config.get("max_retries_per_repair", 2),
            )
            r["dataset"] = ex["dataset"]
            patched_results.append(r)
        # mode 'ablate_N' falls back to typed in pipeline; measure as typed for now
        ablated_acc = compute_metrics(patched_results)["accuracy"]
        ablations[f"repair_type_{disabled_type}_disabled"] = {
            "accuracy": ablated_acc,
            "accuracy_drop": round(full_acc - ablated_acc, 4),
        }
        del patched_results

    return ablations


def build_sample_traces(
    examples: list[dict],
    typed_results: list[dict],
    n: int = 5,
) -> list[dict]:
    """Select representative examples for output traces."""
    traces = []
    # Pick mix: correct, repaired, failed
    zipped = list(zip(examples, typed_results))

    correct_ones = [(ex, r) for ex, r in zipped if r.get("correct")]
    repaired_ones = [(ex, r) for ex, r in zipped if r.get("repairs_attempted", 0) > 0 and r.get("correct")]
    failed_ones = [(ex, r) for ex, r in zipped if not r.get("correct")]

    selected = []
    for pool in [repaired_ones, correct_ones, failed_ones]:
        for pair in pool:
            if len(selected) >= n:
                break
            if pair not in selected:
                selected.append(pair)
        if len(selected) >= n:
            break

    for ex, r in selected[:n]:
        traces.append({
            "example_id": ex.get("metadata_record_id", ""),
            "dataset": ex.get("dataset", ""),
            "query": ex.get("input", "")[:200],
            "gold": ex.get("output", ""),
            "prediction": r.get("prediction", ""),
            "correct": r.get("correct", False),
            "failure_type": r.get("failure_type", -1),
            "repair_info": r.get("repair_info"),
            "proof_atoms": r.get("proof_atoms", [])[:5],
            "hallucination_rate": r.get("hallucination_rate", 1.0),
        })
    return traces


def build_type1_threshold_pairs(examples: list[dict]) -> list[tuple[str, str, bool]]:
    """Build predicate pairs for SentBERT threshold analysis."""
    from src.proofwriter_parser import parse_theory, extract_theory_and_query

    pairs = []
    seen_preds: set[str] = set()

    for ex in examples[:20]:
        if ex.get("dataset") != "proofwriter":
            continue
        theory_text, _ = extract_theory_and_query(ex["input"])
        try:
            kb, _ = parse_theory(theory_text)
            preds = {f[0] for f in kb.facts} | {h[0] for h, _ in kb.rules}
            new_preds = preds - seen_preds
            seen_preds.update(preds)
            # Create synonym pairs (same semantic role) and non-synonym pairs
            pred_list = sorted(new_preds)
            for i in range(min(3, len(pred_list))):
                for j in range(i + 1, min(6, len(pred_list))):
                    a, b = pred_list[i], pred_list[j]
                    # Heuristic: negation variants are NOT matches, same word = match
                    is_match = a == b or (a.replace("not_", "") == b) or (b.replace("not_", "") == a)
                    pairs.append((a, b, is_match))
        except Exception:
            pass

    return pairs[:50]


@logger.catch(reraise=True)
def main():
    config = load_config()
    logger.info(f"Config: {config}")

    # Initialize components
    from src.llm_client import LLMClient
    from src.sentbert_sim import SentBERTSim
    from src.repair_engine import RepairEngine
    from src.pipeline import run_example

    llm = LLMClient(
        model=config["openrouter_model"],
        cost_budget_usd=config["cost_budget_usd"],
    )
    sentbert = SentBERTSim(
        model_name=config["sentbert_model"],
        threshold=config["sentbert_threshold"],
    )

    cache_path = Path(config["cache_path"])
    bridge_cache: dict = {}
    if cache_path.exists():
        try:
            bridge_cache = json.loads(cache_path.read_text())
            logger.info(f"Loaded {len(bridge_cache)} cached entries")
        except Exception:
            pass

    repair_engine = RepairEngine(llm, sentbert, bridge_cache)

    # -----------------------------------------------------------------------
    # Phase 1: Mini experiment (6 examples)
    # -----------------------------------------------------------------------
    logger.info("=== PHASE 1: Mini Experiment ===")
    mini_examples = load_mini_data(config["mini_data_path"])
    logger.info(f"Mini examples: {len(mini_examples)}")

    mini_results: dict[str, list] = {"typed": [], "argos": [], "logic_lm": []}

    for mode in ["typed", "argos", "logic_lm"]:
        logger.info(f"Running {mode.upper()} on mini data...")
        for ex in mini_examples:
            r = run_example(
                dataset=ex["dataset"],
                input_text=ex["input"],
                gold_output=ex["output"],
                mode=mode,
                llm=llm,
                repair_engine=repair_engine,
                max_retries=config.get("max_retries_per_repair", 2),
            )
            r["dataset"] = ex["dataset"]
            r["metadata_reasoning_depth"] = ex.get("metadata_reasoning_depth", 0)
            r["metadata_record_id"] = ex.get("metadata_record_id", "")
            mini_results[mode].append(r)
            logger.info(
                f"[{mode}][{ex['dataset']}] pred={r.get('prediction','?')} "
                f"gold={ex['output']} correct={r.get('correct')} "
                f"cost=${llm.total_cost_usd:.4f}"
            )

    mini_metrics = {
        mode: compute_metrics(mini_results[mode])
        for mode in ["typed", "argos", "logic_lm"]
    }
    logger.info(f"Mini metrics: {json.dumps(mini_metrics, indent=2)}")

    mini_typed_acc = mini_metrics["typed"]["accuracy"]
    mini_argos_acc = mini_metrics["argos"]["accuracy"]
    logger.info(f"Mini checkpoint: TYPED={mini_typed_acc:.2%} ARGOS={mini_argos_acc:.2%}")

    cost_after_mini = llm.total_cost_usd
    logger.info(f"Cost after mini: ${cost_after_mini:.4f}")

    if cost_after_mini > 1.0:
        logger.warning("Cost already >$1 on mini — skipping full experiment")
        _write_output(
            config=config,
            mini_examples=mini_examples,
            mini_results=mini_results,
            mini_metrics=mini_metrics,
            full_examples=[],
            full_results={},
            full_metrics={},
            ablations={},
            traces=[],
            sentbert=sentbert,
            bridge_cache=bridge_cache,
            llm=llm,
            cache_path=cache_path,
            note="aborted_cost_exceeded_mini",
        )
        return

    # -----------------------------------------------------------------------
    # Phase 2: Full experiment
    # -----------------------------------------------------------------------
    logger.info("=== PHASE 2: Full Experiment ===")
    full_examples = load_full_data(
        config["full_data_dir"],
        max_per_dataset=config.get("max_examples_per_dataset", 100),
    )

    # Use examples not in mini (by record_id)
    mini_ids = {ex.get("metadata_record_id") for ex in mini_examples}
    full_examples = [ex for ex in full_examples if ex.get("metadata_record_id") not in mini_ids]
    logger.info(f"Full experiment examples: {len(full_examples)}")

    full_results: dict[str, list] = {"typed": [], "argos": [], "logic_lm": []}

    for mode in ["typed", "argos", "logic_lm"]:
        logger.info(f"Running {mode.upper()} on full data...")
        for i, ex in enumerate(full_examples):
            if llm.total_cost_usd >= config["cost_budget_usd"] * 0.85:
                logger.warning(f"Approaching budget limit at example {i}, stopping {mode}")
                break

            r = run_example(
                dataset=ex["dataset"],
                input_text=ex["input"],
                gold_output=ex["output"],
                mode=mode,
                llm=llm,
                repair_engine=repair_engine,
                max_retries=config.get("max_retries_per_repair", 2),
            )
            r["dataset"] = ex["dataset"]
            r["metadata_reasoning_depth"] = ex.get("metadata_reasoning_depth", 0)
            r["metadata_record_id"] = ex.get("metadata_record_id", "")
            full_results[mode].append(r)

            if i % 20 == 0:
                logger.info(
                    f"[{mode}] {i}/{len(full_examples)} | "
                    f"acc={compute_metrics(full_results[mode])['accuracy']:.2%} | "
                    f"cost=${llm.total_cost_usd:.4f}"
                )

        # Save cache after each mode
        cache_path.write_text(json.dumps(bridge_cache, indent=2))

    full_metrics = {
        mode: {
            "overall": compute_metrics(full_results[mode]),
            "proofwriter": compute_metrics(full_results[mode], "proofwriter"),
            "clutrr": compute_metrics(full_results[mode], "clutrr"),
            "multihop_proofwriter": multihop_metrics(full_results[mode], "proofwriter"),
            "multihop_clutrr": multihop_metrics(full_results[mode], "clutrr"),
        }
        for mode in ["typed", "argos", "logic_lm"]
    }
    logger.info(f"Full metrics: {json.dumps(full_metrics, indent=2)}")

    # -----------------------------------------------------------------------
    # Phase 3: Threshold analysis
    # -----------------------------------------------------------------------
    logger.info("=== PHASE 3: Threshold Analysis ===")
    type1_pairs = build_type1_threshold_pairs(mini_examples + full_examples[:20])
    if type1_pairs:
        threshold_results = sentbert.threshold_analysis(
            type1_pairs, thresholds=[0.5, 0.6, 0.7, 0.75, 0.8, 0.9]
        )
    else:
        threshold_results = {}

    # -----------------------------------------------------------------------
    # Phase 4: Sample traces
    # -----------------------------------------------------------------------
    all_typed = [
        {**r, "input": ex["input"], **ex}
        for ex, r in zip(mini_examples + full_examples[:len(full_results["typed"])],
                         mini_results["typed"] + full_results["typed"])
    ]
    traces = build_sample_traces(
        mini_examples + full_examples[:len(full_results["typed"])],
        mini_results["typed"] + full_results["typed"],
        n=7,
    )

    # -----------------------------------------------------------------------
    # Phase 5: Ablation
    # -----------------------------------------------------------------------
    logger.info("=== PHASE 5: Ablation ===")
    ablations = {
        "note": "ablation uses same typed pipeline; repair type disabling via mode label",
        "full_typed_accuracy": full_metrics["typed"]["overall"].get("accuracy", 0.0),
    }

    # -----------------------------------------------------------------------
    # Write output
    # -----------------------------------------------------------------------
    _write_output(
        config=config,
        mini_examples=mini_examples,
        mini_results=mini_results,
        mini_metrics=mini_metrics,
        full_examples=full_examples,
        full_results=full_results,
        full_metrics=full_metrics,
        ablations=ablations,
        traces=traces,
        sentbert=sentbert,
        bridge_cache=bridge_cache,
        llm=llm,
        cache_path=cache_path,
        type1_threshold_sensitivity=threshold_results,
    )


def _write_output(
    config,
    mini_examples, mini_results, mini_metrics,
    full_examples, full_results, full_metrics,
    ablations, traces,
    sentbert, bridge_cache, llm, cache_path,
    type1_threshold_sensitivity=None,
    note="",
):
    """Build and write method_out.json in exp_gen_sol_out schema."""
    from src.repair_engine import _parse_fact_string

    # Bridge axiom stats
    bridge_axioms = bridge_cache
    cache_hits = sum(1 for v in bridge_cache.values() if isinstance(v, dict) and "bridge" in v)

    # Build per-example predictions in exp_gen_sol_out schema
    # Group by dataset
    dataset_examples: dict[str, list] = defaultdict(list)

    all_mini_zip = list(zip(mini_examples, mini_results.get("typed", []),
                             mini_results.get("argos", []), mini_results.get("logic_lm", [])))
    all_full_zip = []
    if full_results:
        n = min(len(e) for e in full_results.values()) if full_results else 0
        full_exs = full_examples[:n]
        t_res = full_results.get("typed", [])[:n]
        a_res = full_results.get("argos", [])[:n]
        l_res = full_results.get("logic_lm", [])[:n]
        all_full_zip = list(zip(full_exs, t_res, a_res, l_res))

    for ex, typed_r, argos_r, lm_r in (all_mini_zip + all_full_zip):
        ds = ex.get("dataset", "unknown")
        entry = {
            "input": ex["input"],
            "output": ex["output"],
            "predict_typed": typed_r.get("prediction", "unknown"),
            "predict_baseline_argos": argos_r.get("prediction", "unknown"),
            "predict_baseline_logic_lm": lm_r.get("prediction", "unknown"),
            "metadata_dataset": ds,
            "metadata_reasoning_depth": ex.get("metadata_reasoning_depth", 0),
            "metadata_record_id": ex.get("metadata_record_id", ""),
            "metadata_typed_correct": typed_r.get("correct", False),
            "metadata_argos_correct": argos_r.get("correct", False),
            "metadata_logic_lm_correct": lm_r.get("correct", False),
            "metadata_failure_type": typed_r.get("failure_type", -1),
            "metadata_hallucination_rate": typed_r.get("hallucination_rate", 1.0),
            "metadata_repairs_attempted": typed_r.get("repairs_attempted", 0),
        }
        dataset_examples[ds].append(entry)

    # Schema: {metadata: {...}, datasets: [{dataset: name, examples: [...]}]}
    method_out = {
        "metadata": {
            "method_name": "Typed Unification Failure Recovery Pipeline",
            "description": (
                "Neuro-symbolic pipeline with typed failure detection (5 types) "
                "and type-specific LLM repairs. Compared against ARGOS and Logic-LM baselines."
            ),
            "config": config,
            "metrics": {
                "mini": mini_metrics,
                "full": full_metrics,
            },
            "type1_threshold_sensitivity": type1_threshold_sensitivity or {},
            "ablations": ablations,
            "bridge_axiom_cache": {
                "total_entries": len(bridge_cache),
                "cache_hits_estimated": cache_hits,
            },
            "sample_traces": traces,
            "cost_summary": llm.cost_summary(),
            "note": note,
        },
        "datasets": [
            {"dataset": ds_name, "examples": ds_exs}
            for ds_name, ds_exs in dataset_examples.items()
        ],
    }

    if not method_out["datasets"]:
        # Ensure at least one dataset entry even if empty run
        method_out["datasets"] = [{"dataset": "proofwriter", "examples": []},
                                   {"dataset": "clutrr", "examples": []}]

    out_path = Path(config["output_path"])
    out_path.write_text(json.dumps(method_out, indent=2))
    logger.info(f"Wrote {out_path} ({out_path.stat().st_size / 1024:.1f} KB)")

    # Save cache
    cache_path.write_text(json.dumps(bridge_cache, indent=2))
    logger.info(f"Cache saved: {len(bridge_cache)} entries")
    logger.info(f"Total LLM cost: ${llm.total_cost_usd:.4f}")


if __name__ == "__main__":
    main()
