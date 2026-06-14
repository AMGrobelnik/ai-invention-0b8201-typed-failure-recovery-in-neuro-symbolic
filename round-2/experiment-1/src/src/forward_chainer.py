"""
Pure-Python forward-chaining engine (Prolog fallback).

Facts are ground atoms: frozenset of (predicate_str, *arg_strs).
Rules are (head_atom, [body_atom, ...]) where atoms may have variable '?X'.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any

from loguru import logger


Atom = tuple[str, ...]  # e.g. ('furry', 'gary') or ('parent', '?X', '?Y')
Bindings = dict[str, str]


def _is_var(s: str) -> bool:
    return s.startswith("?")


def _unify(atom: Atom, pattern: Atom, bindings: Bindings) -> Bindings | None:
    """Try to unify a ground atom with a pattern (may have vars)."""
    if len(atom) != len(pattern):
        return None
    b = dict(bindings)
    for a_part, p_part in zip(atom, pattern):
        if _is_var(p_part):
            if p_part in b:
                if b[p_part] != a_part:
                    return None
            else:
                b[p_part] = a_part
        else:
            if a_part != p_part:
                return None
    return b


def _apply_bindings(atom: Atom, bindings: Bindings) -> Atom:
    return tuple(bindings.get(part, part) for part in atom)


@dataclass
class KnowledgeBase:
    facts: set[Atom] = field(default_factory=set)
    rules: list[tuple[Atom, list[Atom]]] = field(default_factory=list)
    # span annotations: fact -> source text span
    fact_spans: dict[Atom, str] = field(default_factory=dict)
    # bridge axiom tracking
    bridge_axioms: list[dict] = field(default_factory=list)

    def add_fact(self, atom: Atom, span: str = "") -> None:
        self.facts.add(atom)
        if span:
            self.fact_spans[atom] = span

    def add_rule(self, head: Atom, body: list[Atom]) -> None:
        self.rules.append((head, body))

    def clone(self) -> "KnowledgeBase":
        kb = KnowledgeBase()
        kb.facts = set(self.facts)
        kb.rules = list(self.rules)
        kb.fact_spans = dict(self.fact_spans)
        kb.bridge_axioms = list(self.bridge_axioms)
        return kb


def forward_chain(kb: KnowledgeBase, max_iterations: int = 50) -> KnowledgeBase:
    """Run forward chaining until fixpoint. Returns KB with all derived facts."""
    kb = kb.clone()
    for _ in range(max_iterations):
        new_facts: set[Atom] = set()
        for head_pat, body_pats in kb.rules:
            # Find all ground substitutions satisfying body
            solutions = [{}]
            for body_atom in body_pats:
                next_solutions: list[Bindings] = []
                for bindings in solutions:
                    bound_body = _apply_bindings(body_atom, bindings)
                    # Try matching against all known facts
                    for fact in kb.facts:
                        new_b = _unify(fact, bound_body, bindings)
                        if new_b is not None:
                            next_solutions.append(new_b)
                solutions = next_solutions
                if not solutions:
                    break
            for bindings in solutions:
                derived = _apply_bindings(head_pat, bindings)
                if not _is_var_free(derived):
                    continue
                new_facts.add(derived)
        added = new_facts - kb.facts
        if not added:
            break
        kb.facts.update(added)
    return kb


def _is_var_free(atom: Atom) -> bool:
    return all(not _is_var(part) for part in atom)


@dataclass
class ProofResult:
    success: bool
    answer: str  # 'true', 'false', 'unknown'
    failure_type: int  # 0=none, 1=lex, 2=arity, 3=missing, 4=ontology, 5=scope
    failure_detail: str
    proof_atoms: list[Atom] = field(default_factory=list)
    hallucinated_leaves: int = 0
    grounded_leaves: int = 0


def query(kb: KnowledgeBase, query_atom: Atom, negated: bool = False) -> ProofResult:
    """
    Run forward chain on kb, then check if query_atom is in derived facts.
    Detect failure type if not found.
    """
    derived_kb = forward_chain(kb)

    # Check all predicates in KB for query predicate
    query_pred = query_atom[0]
    kb_preds = {f[0] for f in derived_kb.facts} | {h[0] for h, _ in kb.rules}

    if query_pred not in kb_preds and query_pred not in {f[0] for f in derived_kb.facts}:
        # Type 1: predicate not in KB at all
        return ProofResult(
            success=False,
            answer="false",
            failure_type=1,
            failure_detail=f"Unknown predicate '{query_pred}' not in KB. KB predicates: {sorted(kb_preds)[:10]}",
        )

    if query_atom in derived_kb.facts:
        if negated:
            return ProofResult(success=True, answer="false", failure_type=0, failure_detail="",
                               proof_atoms=[query_atom])
        return ProofResult(success=True, answer="true", failure_type=0, failure_detail="",
                           proof_atoms=[query_atom])

    # Not found - Type 3: missing fact/rule
    # But check for arity mismatch (query has different arity than KB facts of same pred)
    same_pred_facts = [f for f in derived_kb.facts if f[0] == query_pred]
    if same_pred_facts and len(same_pred_facts[0]) != len(query_atom):
        return ProofResult(
            success=False,
            answer="false",
            failure_type=2,
            failure_detail=f"Arity mismatch: query has {len(query_atom)-1} args, KB has {len(same_pred_facts[0])-1}",
        )

    # Clean failure: predicate known but this ground instance not derived
    if negated:
        return ProofResult(success=True, answer="true", failure_type=0, failure_detail="")

    return ProofResult(
        success=False,
        answer="false",
        failure_type=3,
        failure_detail=f"Subgoal '{' '.join(query_atom)}' not derivable. Derived {len(derived_kb.facts)} facts.",
    )
