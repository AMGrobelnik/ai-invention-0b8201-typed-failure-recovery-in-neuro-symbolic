"""
Three pipeline implementations:
  - TYPED: our method (typed failure detection + targeted repair)
  - ARGOS: baseline 1 (single undifferentiated abductive repair)
  - LOGIC_LM: baseline 2 (raw error forwarding to LLM)
"""

from __future__ import annotations

import re
import time
from typing import Any, TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from src.llm_client import LLMClient
    from src.repair_engine import RepairEngine
    from src.sentbert_sim import SentBERTSim

from src.forward_chainer import KnowledgeBase, query as fc_query, forward_chain, Atom
from src.proofwriter_parser import parse_theory, parse_query, extract_theory_and_query
from src.clutrr_parser import solve_clutrr, extract_story_and_query
from src.repair_engine import RepairEngine, _parse_fact_string

SYSTEM_PROMPT = (
    "You are a neuro-symbolic reasoning assistant. "
    "Answer only with the requested format."
)


# ---------------------------------------------------------------------------
# ProofWriter solver
# ---------------------------------------------------------------------------

def _solve_proofwriter(
    input_text: str,
    gold_output: str,
    mode: str,
    llm: "LLMClient",
    repair_engine: "RepairEngine",
    max_retries: int = 2,
) -> dict[str, Any]:
    theory_text, query_text = extract_theory_and_query(input_text)
    if not theory_text or not query_text:
        return _proofwriter_llm_direct(input_text, llm, mode)

    kb, parse_failures = parse_theory(theory_text)
    query_atom, negated = parse_query(query_text)

    if query_atom == (("unknown", "unknown"), False)[0]:
        return _proofwriter_llm_direct(input_text, llm, mode)

    source_text = theory_text + " " + query_text
    result = fc_query(kb, query_atom, negated)

    repair_info = None
    repairs_attempted = 0

    if mode == "typed":
        # Typed repair loop
        for attempt in range(max_retries):
            if result.success or result.failure_type == 0:
                break
            ft = result.failure_type
            logger.debug(f"Failure type {ft}: {result.failure_detail[:80]}")
            repairs_attempted += 1
            if ft == 1:
                kb, repair_info = repair_engine.repair_type1(kb, query_atom, source_text, result.failure_detail)
            elif ft == 2:
                kb, repair_info = repair_engine.repair_type2(kb, query_atom, source_text, result.failure_detail)
            elif ft == 3:
                kb, repair_info = repair_engine.repair_type3(kb, query_atom, source_text, result.failure_detail)
            elif ft == 4:
                kb, repair_info = repair_engine.repair_type4(kb, query_atom, source_text, result.failure_detail)
            else:
                kb, repair_info = repair_engine.repair_generic(kb, query_atom, source_text, result.failure_detail)

            if repair_info and repair_info.get("success"):
                result = fc_query(kb, query_atom, negated)
            else:
                break

    elif mode == "argos":
        # ARGOS: single generic abductive call on any failure
        if not result.success and result.failure_type != 0:
            repairs_attempted += 1
            kb, repair_info = repair_engine.repair_generic(kb, query_atom, source_text, result.failure_detail)
            if repair_info and repair_info.get("success"):
                result = fc_query(kb, query_atom, negated)

    elif mode == "logic_lm":
        # Logic-LM: forward raw error to LLM for answer
        if not result.success and result.failure_type != 0:
            repairs_attempted += 1
            answer = _logic_lm_repair(
                kb, query_atom, negated, source_text, result.failure_detail, llm
            )
            return {
                "prediction": answer,
                "failure_type": result.failure_type,
                "failure_detail": result.failure_detail,
                "repair_info": {"mode": "logic_lm", "success": answer in ("true", "false")},
                "repairs_attempted": 1,
                "proof_atoms": [],
                "hallucination_rate": 1.0,
                "parse_failures": len(parse_failures),
            }

    # Determine prediction
    if result.success:
        prediction = result.answer  # 'true' or 'false'
    else:
        # forward-chain gives 'false' on clean failure; convert to prediction
        if negated:
            # no derivation of base attr → negation likely true
            prediction = "true"
        else:
            prediction = "false"

    # Compute hallucination rate
    derived_kb = forward_chain(kb)
    grounded = sum(1 for a in result.proof_atoms if a in kb.fact_spans or a in kb.facts)
    total = max(len(result.proof_atoms), 1)
    hallucination_rate = max(0.0, (total - grounded) / total)

    return {
        "prediction": prediction,
        "failure_type": result.failure_type,
        "failure_detail": result.failure_detail,
        "repair_info": repair_info,
        "repairs_attempted": repairs_attempted,
        "proof_atoms": [list(a) for a in result.proof_atoms],
        "hallucination_rate": hallucination_rate,
        "parse_failures": len(parse_failures),
    }


def _proofwriter_llm_direct(input_text: str, llm: "LLMClient", mode: str) -> dict:
    """Fallback: ask LLM directly for true/false."""
    try:
        prompt = (
            f"Given the following logical theory, determine if the query is true or false.\n"
            f"{input_text}\n\nAnswer with exactly 'true' or 'false'."
        )
        response = llm.call(prompt, system=SYSTEM_PROMPT, max_tokens=10, temperature=0.0,
                            cache_key=f"direct:{hash(input_text)}")
        prediction = "true" if "true" in response.lower() else "false"
    except Exception as e:
        logger.error(f"LLM direct call failed: {e}")
        prediction = "false"

    return {
        "prediction": prediction,
        "failure_type": -1,
        "failure_detail": "parse_error",
        "repair_info": {"mode": mode, "fallback": True},
        "repairs_attempted": 0,
        "proof_atoms": [],
        "hallucination_rate": 1.0,
        "parse_failures": 1,
    }


def _logic_lm_repair(
    kb: KnowledgeBase,
    query_atom: Atom,
    negated: bool,
    source_text: str,
    error_msg: str,
    llm: "LLMClient",
) -> str:
    """Logic-LM style: forward raw error to LLM and ask for corrected answer."""
    try:
        prompt = (
            f"A logic proof system returned this error:\n{error_msg}\n\n"
            f"Query: '{' '.join(query_atom)}' (negated={negated})\n"
            f"Source: \"{source_text[:300]}\"\n\n"
            f"Is the query true or false? Answer with 'true' or 'false' only."
        )
        response = llm.call(
            prompt, system=SYSTEM_PROMPT, max_tokens=10,
            cache_key=f"logicLM:{hash(source_text)}{' '.join(query_atom)}"
        )
        return "true" if "true" in response.lower() else "false"
    except Exception:
        return "false"


# ---------------------------------------------------------------------------
# CLUTRR solver
# ---------------------------------------------------------------------------

def _solve_clutrr(
    input_text: str,
    gold_output: str,
    mode: str,
    llm: "LLMClient",
    repair_engine: "RepairEngine",
    max_retries: int = 2,
) -> dict[str, Any]:
    story, query_str = extract_story_and_query(input_text)
    if not story or not query_str:
        return _clutrr_llm_direct(input_text, llm)

    result = solve_clutrr(story, query_str)
    repair_info = None
    repairs_attempted = 0

    if result["failure_type"] != 0:
        if mode == "typed":
            # Type 1: entity not found → LLM to extract entity
            if result["failure_type"] == 1:
                repairs_attempted += 1
                answer, repair_info = _clutrr_repair_type1(story, query_str, result, llm)
                if answer:
                    result = {"answer": answer, "failure_type": 0, "failure_detail": "", "hops": 1, "method": "typed_repair", "relations_found": result["relations_found"]}
            elif result["failure_type"] == 3:
                # Missing path — use LLM
                repairs_attempted += 1
                answer, repair_info = _clutrr_llm_repair(story, query_str, result["failure_detail"], llm)
                if answer:
                    result = {"answer": answer, "failure_type": 0, "failure_detail": "", "hops": 1, "method": "typed_repair_t3", "relations_found": result["relations_found"]}

        elif mode == "argos":
            repairs_attempted += 1
            answer, repair_info = _clutrr_llm_repair(story, query_str, result["failure_detail"], llm)
            if answer:
                result = {"answer": answer, "failure_type": 0, "failure_detail": "", "hops": 1, "method": "argos_repair", "relations_found": result["relations_found"]}

        elif mode == "logic_lm":
            repairs_attempted += 1
            prompt = (
                f"Kinship graph traversal failed: {result['failure_detail']}\n"
                f"Story: {story[:300]}\nQuery: {query_str}\n\n"
                f"What is the kinship relationship? Answer with one word (e.g., grandson, uncle)."
            )
            try:
                answer = llm.call(prompt, system=SYSTEM_PROMPT, max_tokens=20,
                                  cache_key=f"logicLMclutrr:{hash(story)}{query_str}").strip().lower()
                repair_info = {"mode": "logic_lm"}
                if answer:
                    result = {"answer": answer, "failure_type": 0, "failure_detail": "", "hops": 1, "method": "logic_lm", "relations_found": result["relations_found"]}
            except Exception:
                pass

    prediction = result.get("answer", "unknown")
    hallucination_rate = 1.0 if result["failure_type"] != 0 else 0.0
    if repair_info and repair_info.get("success"):
        hallucination_rate = 0.5  # partial grounding via LLM

    return {
        "prediction": prediction,
        "failure_type": result["failure_type"],
        "failure_detail": result.get("failure_detail", ""),
        "repair_info": repair_info,
        "repairs_attempted": repairs_attempted,
        "hops": result.get("hops", 0),
        "method": result.get("method", "graph_traversal"),
        "hallucination_rate": hallucination_rate,
        "relations_found": len(result.get("relations_found", {})),
    }


def _clutrr_repair_type1(
    story: str, query_str: str, result: dict, llm: "LLMClient"
) -> tuple[str, dict]:
    try:
        prompt = (
            f"Extract all kinship relationships from this story and answer the query.\n"
            f"Story: {story[:400]}\nQuery: {query_str}\n\n"
            f"Answer with just the kinship term (e.g., grandson, uncle, mother)."
        )
        answer = llm.call(prompt, system=SYSTEM_PROMPT, max_tokens=20,
                          cache_key=f"t1clutrr:{hash(story)}{query_str}").strip().lower()
        return answer, {"repair_type": 1, "success": True}
    except Exception as e:
        return "", {"repair_type": 1, "success": False, "reason": str(e)}


def _clutrr_llm_repair(
    story: str, query_str: str, error: str, llm: "LLMClient"
) -> tuple[str, dict]:
    try:
        prompt = (
            f"Story: {story[:400]}\nQuery: {query_str}\n\n"
            f"Determine the kinship relationship. Answer with one word only."
        )
        answer = llm.call(prompt, system=SYSTEM_PROMPT, max_tokens=20,
                          cache_key=f"clutrr_repair:{hash(story)}{query_str}").strip().lower()
        return answer, {"repair_type": 3, "success": True}
    except Exception as e:
        return "", {"repair_type": 3, "success": False, "reason": str(e)}


def _clutrr_llm_direct(input_text: str, llm: "LLMClient") -> dict:
    try:
        prompt = f"{input_text}\n\nAnswer with the kinship relationship (one word only)."
        answer = llm.call(prompt, system=SYSTEM_PROMPT, max_tokens=20,
                          cache_key=f"clutrr_direct:{hash(input_text)}").strip().lower()
    except Exception:
        answer = "unknown"
    return {
        "prediction": answer, "failure_type": -1, "failure_detail": "parse_error",
        "repair_info": {"fallback": True}, "repairs_attempted": 0,
        "hops": 0, "method": "llm_direct", "hallucination_rate": 1.0, "relations_found": 0,
    }


# ---------------------------------------------------------------------------
# Main pipeline dispatcher
# ---------------------------------------------------------------------------

def run_example(
    dataset: str,
    input_text: str,
    gold_output: str,
    mode: str,
    llm: "LLMClient",
    repair_engine: "RepairEngine",
    max_retries: int = 2,
) -> dict[str, Any]:
    """Run one example through the specified pipeline mode."""
    t0 = time.perf_counter()
    try:
        if dataset == "proofwriter":
            result = _solve_proofwriter(input_text, gold_output, mode, llm, repair_engine, max_retries)
        elif dataset == "clutrr":
            result = _solve_clutrr(input_text, gold_output, mode, llm, repair_engine, max_retries)
        else:
            result = {"prediction": "unknown", "failure_type": -1, "failure_detail": f"unknown dataset: {dataset}", "hallucination_rate": 1.0}
    except Exception as e:
        logger.error(f"Pipeline error ({mode}/{dataset}): {e}")
        result = {"prediction": "unknown", "failure_type": -1, "failure_detail": str(e), "hallucination_rate": 1.0, "repairs_attempted": 0}

    result["elapsed_s"] = round(time.perf_counter() - t0, 3)
    result["correct"] = _is_correct(result.get("prediction", "unknown"), gold_output, dataset)
    return result


def _is_correct(prediction: str, gold: str, dataset: str) -> bool:
    pred = prediction.strip().lower()
    gold = gold.strip().lower()
    if dataset == "proofwriter":
        return pred == gold
    elif dataset == "clutrr":
        # Normalize
        pred = re.sub(r"[-_\s]+", " ", pred).strip()
        gold = re.sub(r"[-_\s]+", " ", gold).strip()
        return pred == gold or pred in gold or gold in pred
    return pred == gold
