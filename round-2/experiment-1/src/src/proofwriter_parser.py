"""
Parse ProofWriter natural-language theory + query into KB atoms and rules.

Handles the controlled NL subset used in ProofWriter/RuleTaker:
  Facts:    'Gary is furry.'               → ('furry', 'gary')
  Neg facts:'Gary is not smart.'           → ('not_smart', 'gary')
  Rules:    'If someone is X then they...' → universal rule
            'All X things are Y.'          → universal rule
            'If A is X and A is Y then...' → entity-specific rule
            'X, Y people are Z.'           → conjunctive antecedent
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from src.forward_chainer import Atom, KnowledgeBase


def _norm(s: str) -> str:
    """Normalize a word/phrase to identifier."""
    return re.sub(r"[^a-z0-9_]", "_", s.strip().lower()).strip("_")


def _predicate(attr: str, negated: bool = False) -> str:
    p = _norm(attr)
    return f"not_{p}" if negated else p


def _parse_entity_fact(sent: str) -> tuple[str, str, bool] | None:
    """Parse 'Gary is [not] X' → (entity, attr, negated)."""
    m = re.match(
        r"^([A-Z][A-Za-z]*)\s+is\s+(not\s+)?([a-z][a-z\s]*?)\.?\s*$", sent
    )
    if m:
        entity = _norm(m.group(1))
        negated = bool(m.group(2))
        attr = _norm(m.group(3).strip())
        return entity, attr, negated
    return None


def _parse_conjunctive_rule(sent: str) -> tuple | None:
    """
    Parse 'Smart, red people are rough.' or 'Furry, young things are nice.'
    → head: ('rough', '?X')  body: [('smart', '?X'), ('red', '?X')]
    """
    m = re.match(
        r"^((?:[A-Za-z]+,\s+)*[A-Za-z]+)\s+(?:people|things?)\s+are\s+([a-z][a-z\s]*)\.?\s*$",
        sent,
        re.IGNORECASE,
    )
    if not m:
        return None
    antecedents_raw = m.group(1)
    consequent = _norm(m.group(2).strip())
    attrs = [_norm(a.strip()) for a in re.split(r",\s*", antecedents_raw)]
    if not attrs or not consequent:
        return None
    body = [(_predicate(a), "?X") for a in attrs]
    head = (_predicate(consequent), "?X")
    return head, body


def _parse_all_rule(sent: str) -> tuple | None:
    """
    Parse 'All rough things are white.' or 'All young people are furry.'
    → head: ('white', '?X')  body: [('rough', '?X')]
    """
    m = re.match(
        r"^All\s+([a-z][a-z\s]*?)\s+(?:things?|people)\s+are\s+([a-z][a-z\s]*)\.?\s*$",
        sent,
        re.IGNORECASE,
    )
    if m:
        ante = _norm(m.group(1).strip())
        cons = _norm(m.group(2).strip())
        return (_predicate(cons), "?X"), [(_predicate(ante), "?X")]

    # 'Rough things are white.'
    m = re.match(
        r"^([A-Za-z][a-z\s]*?)\s+things?\s+are\s+([a-z][a-z\s]*)\.?\s*$", sent
    )
    if m:
        ante = _norm(m.group(1).strip())
        cons = _norm(m.group(2).strip())
        return (_predicate(cons), "?X"), [(_predicate(ante), "?X")]

    return None


def _parse_if_then(sent: str) -> tuple | None:
    """
    Parse various 'If ... then ...' patterns.
    Returns (head_atom, body_atoms) or None.
    """
    sent = sent.strip().rstrip(".")

    # Universal: 'If someone is X then they are Y'
    m = re.match(
        r"[Ii]f\s+someone\s+is\s+(not\s+)?([a-z][a-z\s]*?)\s+then\s+they\s+are\s+(not\s+)?([a-z][a-z\s]*?)\.?\s*$",
        sent,
    )
    if m:
        ante_neg = bool(m.group(1))
        ante = _norm(m.group(2).strip())
        cons_neg = bool(m.group(3))
        cons = _norm(m.group(4).strip())
        return (_predicate(cons, cons_neg), "?X"), [(_predicate(ante, ante_neg), "?X")]

    # Universal conjunction: 'If someone is X and [someone is / they are] Y then they are Z'
    m = re.match(
        r"[Ii]f\s+someone\s+is\s+(not\s+)?([a-z][a-z\s]*?)\s+and\s+(?:someone\s+is|they\s+are)\s+(not\s+)?([a-z][a-z\s]*?)\s+then\s+they\s+are\s+(not\s+)?([a-z][a-z\s]*?)\.?\s*$",
        sent,
    )
    if m:
        a1_neg, a1 = bool(m.group(1)), _norm(m.group(2).strip())
        a2_neg, a2 = bool(m.group(3)), _norm(m.group(4).strip())
        c_neg, c = bool(m.group(5)), _norm(m.group(6).strip())
        body = [(_predicate(a1, a1_neg), "?X"), (_predicate(a2, a2_neg), "?X")]
        return (_predicate(c, c_neg), "?X"), body

    # Entity-specific: 'If Gary is X then Gary is Y'
    m = re.match(
        r"[Ii]f\s+([A-Z][A-Za-z]*)\s+is\s+(not\s+)?([a-z][a-z\s]*?)\s+then\s+\1\s+is\s+(not\s+)?([a-z][a-z\s]*?)\.?\s*$",
        sent,
    )
    if m:
        entity = _norm(m.group(1))
        a_neg, a = bool(m.group(2)), _norm(m.group(3).strip())
        c_neg, c = bool(m.group(4)), _norm(m.group(5).strip())
        return (_predicate(c, c_neg), entity), [(_predicate(a, a_neg), entity)]

    # Entity conjunction: 'If Gary is X and Gary is [not] Y then Gary is [not] Z'
    m = re.match(
        r"[Ii]f\s+([A-Z][A-Za-z]*)\s+is\s+(not\s+)?([a-z][a-z\s]*?)\s+and\s+\1\s+is\s+(not\s+)?([a-z][a-z\s]*?)\s+then\s+\1\s+is\s+(not\s+)?([a-z][a-z\s]*?)\.?\s*$",
        sent,
    )
    if m:
        entity = _norm(m.group(1))
        a1_neg, a1 = bool(m.group(2)), _norm(m.group(3).strip())
        a2_neg, a2 = bool(m.group(4)), _norm(m.group(5).strip())
        c_neg, c = bool(m.group(6)), _norm(m.group(7).strip())
        body = [(_predicate(a1, a1_neg), entity), (_predicate(a2, a2_neg), entity)]
        return (_predicate(c, c_neg), entity), body

    # Universal three-part conjunction
    m = re.match(
        r"[Ii]f\s+someone\s+is\s+(not\s+)?([a-z][a-z\s]*?)\s+and\s+(?:someone\s+is|they\s+are)\s+(not\s+)?([a-z][a-z\s]*?)\s+and\s+(?:someone\s+is|they\s+are)\s+(not\s+)?([a-z][a-z\s]*?)\s+then\s+they\s+are\s+(not\s+)?([a-z][a-z\s]*?)\.?\s*$",
        sent,
    )
    if m:
        parts = [(bool(m.group(2*i-1)), _norm(m.group(2*i).strip())) for i in range(1, 4)]
        c_neg, c = bool(m.group(7)), _norm(m.group(8).strip())
        body = [(_predicate(a, neg), "?X") for neg, a in parts]
        return (_predicate(c, c_neg), "?X"), body

    return None


def parse_theory(theory_text: str) -> "KnowledgeBase":
    """Parse ProofWriter theory text into a KnowledgeBase."""
    from src.forward_chainer import KnowledgeBase

    kb = KnowledgeBase()
    parse_failures = []

    # Split on '. ' or '.\n' but be careful with sentence boundaries
    sentences = re.split(r"\.\s+", theory_text.strip())
    sentences = [s.strip().rstrip(".") for s in sentences if s.strip()]

    for raw_sent in sentences:
        sent = raw_sent.strip()
        if not sent:
            continue

        parsed = False

        # Try if-then rule
        if re.match(r"[Ii]f\s+", sent):
            result = _parse_if_then(sent)
            if result:
                head, body = result
                kb.add_rule(head, body)
                parsed = True

        # Try universal / conjunctive rule
        if not parsed and re.search(r"\bpeople\b|\bthings?\b", sent, re.IGNORECASE):
            result = _parse_all_rule(sent) or _parse_conjunctive_rule(sent)
            if result:
                head, body = result
                kb.add_rule(head, body)
                parsed = True

        # Try entity fact
        if not parsed:
            result = _parse_entity_fact(sent)
            if result:
                entity, attr, negated = result
                atom = (_predicate(attr, negated), entity)
                kb.add_fact(atom, span=sent)
                parsed = True

        if not parsed:
            parse_failures.append(sent)
            logger.debug(f"Parse failure: '{sent[:80]}'")

    if parse_failures:
        logger.debug(f"Unparsed sentences ({len(parse_failures)}): {parse_failures[:3]}")

    return kb, parse_failures


def parse_query(query_text: str) -> tuple["Atom", bool]:
    """
    Parse ProofWriter query string → (query_atom, negated).
    e.g. 'Gary is white.' → (('white', 'gary'), False)
         'Gary is not nice.' → (('nice', 'gary'), True)
    """
    query_text = query_text.strip().rstrip(".")
    m = re.match(
        r"^([A-Z][A-Za-z]*)\s+is\s+(not\s+)?([a-z][a-z\s]*)$", query_text
    )
    if m:
        entity = _norm(m.group(1))
        negated = bool(m.group(2))
        attr = _norm(m.group(3).strip())
        # Represent as positive predicate lookup: 'not_X' when negated
        atom = (_predicate(attr, False), entity)
        return atom, negated
    logger.warning(f"Could not parse query: '{query_text}'")
    return (("unknown", "unknown"), False)


def extract_theory_and_query(input_text: str) -> tuple[str, str]:
    """Extract Theory: and Query: parts from ProofWriter input."""
    theory = ""
    query = ""
    if "Theory:" in input_text:
        parts = input_text.split("Theory:", 1)[1]
        if "Query:" in parts:
            theory_part, query_part = parts.split("Query:", 1)
            theory = theory_part.strip()
            query = query_part.strip()
        else:
            theory = parts.strip()
    elif "Query:" in input_text:
        query = input_text.split("Query:", 1)[1].strip()
    else:
        theory = input_text.strip()
    return theory, query
