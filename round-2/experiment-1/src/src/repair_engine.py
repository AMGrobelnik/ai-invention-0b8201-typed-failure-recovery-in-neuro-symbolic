"""
Typed repair engine: applies LLM repairs based on failure type.
Also implements ARGOS (Type 0: undifferentiated) and Logic-LM (raw error) baselines.
"""

from __future__ import annotations

import json
import re
from typing import Any, TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from src.llm_client import LLMClient
    from src.forward_chainer import KnowledgeBase, Atom


SYSTEM_PROMPT = (
    "You are a neuro-symbolic reasoning assistant. "
    "Output ONLY the requested information in the exact format specified. "
    "No explanations unless asked."
)


def _extract_json_block(text: str) -> dict | None:
    """Extract first JSON object from LLM response."""
    m = re.search(r"\{[^{}]*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass
    return None


class RepairEngine:
    def __init__(self, llm: "LLMClient", sentbert_sim, cache: dict):
        self.llm = llm
        self.sentbert_sim = sentbert_sim
        self.cache = cache  # persistent bridge axiom cache

    # ------------------------------------------------------------------
    # Type 1: Lexical predicate mismatch (unknown predicate in KB)
    # ------------------------------------------------------------------
    def repair_type1(
        self,
        kb: "KnowledgeBase",
        query_atom: "Atom",
        source_text: str,
        failure_detail: str,
    ) -> tuple["KnowledgeBase", dict]:
        """
        Use SentBERT to find similar predicates, then LLM to generate bridge axiom.
        Returns updated KB and repair info.
        """
        unknown_pred = query_atom[0]
        kb_preds = sorted({f[0] for f in kb.facts} | {h[0] for h, _ in kb.rules})

        # SentBERT similarity
        similar = self.sentbert_sim.find_similar(unknown_pred, kb_preds)
        cache_key = f"bridge:{unknown_pred}:{similar[0][0] if similar else 'none'}"

        if cache_key in self.cache:
            bridge_info = self.cache[cache_key]
            logger.info(f"Bridge axiom cache HIT: {cache_key}")
        else:
            if not similar:
                return kb, {"repair_type": 1, "success": False, "reason": "no similar predicates"}

            top_matches = [f"{p} (sim={s:.2f})" for p, s in similar[:3]]
            prompt = (
                f"The predicate '{unknown_pred}' is not in the knowledge base.\n"
                f"Most similar KB predicates: {', '.join(top_matches)}\n"
                f"Source text: \"{source_text[:300]}\"\n\n"
                f"If '{unknown_pred}' and '{similar[0][0]}' refer to the same property, "
                f"generate a bridge rule. Otherwise state no bridge is possible.\n\n"
                f"Respond in JSON: {{\"bridge\": \"source_pred -> target_pred\" or null, "
                f"\"span\": \"text supporting this\"}}"
            )
            response = self.llm.call(prompt, system=SYSTEM_PROMPT, max_tokens=150)
            parsed = _extract_json_block(response)
            if not parsed or not parsed.get("bridge"):
                return kb, {"repair_type": 1, "success": False, "reason": "LLM said no bridge"}

            bridge_info = {"bridge": parsed["bridge"], "span": parsed.get("span", "")}
            self.cache[cache_key] = bridge_info

        # Apply bridge: add alias rule
        try:
            bridge_str = bridge_info["bridge"]
            src, tgt = [x.strip() for x in bridge_str.split("->")]
            # Add rule: unknown_pred(X) :- known_pred(X)
            kb = kb.clone()
            kb.add_rule((unknown_pred, "?X"), [(tgt, "?X")])
            kb.bridge_axioms.append({
                "source_pred": unknown_pred,
                "target_pred": tgt,
                "span": bridge_info.get("span", ""),
                "cache_key": cache_key,
            })
            logger.info(f"Type 1 repair: added bridge {unknown_pred} → {tgt}")
            return kb, {"repair_type": 1, "success": True, "bridge": bridge_str}
        except Exception as e:
            return kb, {"repair_type": 1, "success": False, "reason": str(e)}

    # ------------------------------------------------------------------
    # Type 2: Arity mismatch
    # ------------------------------------------------------------------
    def repair_type2(
        self,
        kb: "KnowledgeBase",
        query_atom: "Atom",
        source_text: str,
        failure_detail: str,
    ) -> tuple["KnowledgeBase", dict]:
        pred = query_atom[0]
        prompt = (
            f"The predicate '{pred}' has an arity mismatch.\n"
            f"Detail: {failure_detail}\n"
            f"Source: \"{source_text[:200]}\"\n\n"
            f"Respond in JSON: {{\"corrected_predicate\": \"pred_name\", \"arity\": <int>}}"
        )
        response = self.llm.call(prompt, system=SYSTEM_PROMPT, max_tokens=100)
        parsed = _extract_json_block(response)
        if not parsed:
            return kb, {"repair_type": 2, "success": False, "reason": "no JSON"}
        # We can't easily fix arity in forward chainer without rewriting facts
        # Mark as attempted
        return kb, {
            "repair_type": 2,
            "success": False,
            "reason": "arity repair not implementable without schema change",
            "suggestion": parsed,
        }

    # ------------------------------------------------------------------
    # Type 3: Missing domain fact (abductive)
    # ------------------------------------------------------------------
    def repair_type3(
        self,
        kb: "KnowledgeBase",
        query_atom: "Atom",
        source_text: str,
        failure_detail: str,
    ) -> tuple["KnowledgeBase", dict]:
        """Ask LLM to supply a missing fact extracted from source text."""
        pred = query_atom[0]
        entity = query_atom[1] if len(query_atom) > 1 else "unknown"
        cache_key = f"abduct:{pred}:{entity}:{hash(source_text[:200])}"

        if cache_key in self.cache:
            abduced = self.cache[cache_key]
            logger.info(f"Abductive cache HIT: {cache_key}")
        else:
            prompt = (
                f"The following fact cannot be derived: '{' '.join(query_atom)}'.\n"
                f"Knowledge base has {len(kb.facts)} facts.\n"
                f"Source text: \"{source_text[:400]}\"\n\n"
                f"What minimal fact from the source text would allow '{' '.join(query_atom)}' to be true? "
                f"Only add facts explicitly supported by the text.\n\n"
                f"Respond in JSON: {{\"new_fact\": \"predicate(entity)\" or null, "
                f"\"span\": \"supporting text\"}}"
            )
            response = self.llm.call(prompt, system=SYSTEM_PROMPT, max_tokens=150)
            parsed = _extract_json_block(response)
            if not parsed or not parsed.get("new_fact"):
                return kb, {"repair_type": 3, "success": False, "reason": "LLM no fact"}
            abduced = {"new_fact": parsed["new_fact"], "span": parsed.get("span", "")}
            self.cache[cache_key] = abduced

        # Try to parse new_fact: "predicate(entity)" or "entity is attr"
        new_fact_str = abduced["new_fact"]
        atom = _parse_fact_string(new_fact_str)
        if atom is None:
            return kb, {"repair_type": 3, "success": False, "reason": f"Cannot parse: {new_fact_str}"}

        kb = kb.clone()
        kb.add_fact(atom, span=abduced.get("span", ""))
        logger.info(f"Type 3 repair: added abduced fact {atom}")
        return kb, {"repair_type": 3, "success": True, "new_fact": new_fact_str, "atom": atom}

    # ------------------------------------------------------------------
    # Type 4: Entity re-identification
    # ------------------------------------------------------------------
    def repair_type4(
        self,
        kb: "KnowledgeBase",
        query_atom: "Atom",
        source_text: str,
        failure_detail: str,
    ) -> tuple["KnowledgeBase", dict]:
        pred = query_atom[0]
        entity = query_atom[1] if len(query_atom) > 1 else "unknown"
        prompt = (
            f"Entity '{entity}' may be misidentified in the knowledge base.\n"
            f"Predicate checked: '{pred}'.\n"
            f"Source text: \"{source_text[:300]}\"\n\n"
            f"What is the correct entity name or type for this predicate? "
            f"Respond in JSON: {{\"correct_entity\": \"name\", \"reason\": \"...\"}}"
        )
        response = self.llm.call(prompt, system=SYSTEM_PROMPT, max_tokens=150)
        parsed = _extract_json_block(response)
        if not parsed:
            return kb, {"repair_type": 4, "success": False, "reason": "no JSON"}
        correct_entity = parsed.get("correct_entity", "")
        if correct_entity and correct_entity != entity:
            kb = kb.clone()
            # Add alias: treat 'entity' facts as also being 'correct_entity' facts
            kb_preds = {f[0] for f in kb.facts if f[1] == entity}
            for p in kb_preds:
                from src.forward_chainer import _norm
                kb.add_rule((p, entity), [(p, correct_entity)])
            logger.info(f"Type 4 repair: aliased {entity} → {correct_entity}")
            return kb, {"repair_type": 4, "success": True, "correct_entity": correct_entity}
        return kb, {"repair_type": 4, "success": False, "reason": "no correction needed"}

    # ------------------------------------------------------------------
    # Type 5 / Generic: undifferentiated abductive (ARGOS-style)
    # ------------------------------------------------------------------
    def repair_generic(
        self,
        kb: "KnowledgeBase",
        query_atom: "Atom",
        source_text: str,
        failure_detail: str,
        raw_error: str = "",
    ) -> tuple["KnowledgeBase", dict]:
        """Generic abductive repair — no type classification."""
        cache_key = f"generic:{' '.join(query_atom)}:{hash(source_text[:200])}"
        if cache_key in self.cache:
            result = self.cache[cache_key]
        else:
            prompt = (
                f"Proof failed for: '{' '.join(query_atom)}'.\n"
                f"Error: {failure_detail or raw_error}\n"
                f"Source text: \"{source_text[:400]}\"\n\n"
                f"Supply missing facts or rules to make this query succeed. "
                f"Respond in JSON: {{\"new_facts\": [\"fact1\", ...], \"span\": \"supporting text\"}}"
            )
            response = self.llm.call(prompt, system=SYSTEM_PROMPT, max_tokens=200)
            parsed = _extract_json_block(response)
            result = parsed or {"new_facts": []}
            self.cache[cache_key] = result

        new_facts = result.get("new_facts", [])
        kb = kb.clone()
        added = []
        for fact_str in new_facts[:3]:  # limit to 3
            atom = _parse_fact_string(fact_str)
            if atom:
                kb.add_fact(atom, span=result.get("span", ""))
                added.append(atom)
        return kb, {"repair_type": 0, "success": bool(added), "new_facts": new_facts, "added": added}


def _parse_fact_string(fact_str: str) -> "Atom | None":
    """Parse 'pred(entity)' or 'entity is attr' → Atom."""
    fact_str = fact_str.strip().rstrip(".")
    # pred(entity) format
    m = re.match(r"^(\w+)\(([^)]+)\)$", fact_str)
    if m:
        pred = m.group(1).lower()
        args = [a.strip().lower() for a in m.group(2).split(",")]
        return tuple([pred] + args)
    # 'entity is attr' format
    m = re.match(r"^([A-Za-z]+)\s+is\s+(not\s+)?([a-z][a-z\s]*)$", fact_str)
    if m:
        entity = m.group(1).lower()
        negated = bool(m.group(2))
        attr = re.sub(r"\s+", "_", m.group(3).strip())
        pred = f"not_{attr}" if negated else attr
        return (pred, entity)
    return None
