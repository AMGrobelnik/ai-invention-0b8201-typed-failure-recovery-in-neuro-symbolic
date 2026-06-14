"""
CLUTRR kinship reasoning via graph traversal + composition table.
No Prolog needed — proof_state gives explicit chain.
"""

from __future__ import annotations

import ast
import re
from typing import Any

from loguru import logger


# Kinship composition table: compose(rel1, rel2) = result
# rel1 is the relation from A→B, rel2 is B→C, result is A→C
KINSHIP_COMPOSITION: dict[tuple[str, str], str] = {
    # parent chains
    ("father", "father"): "paternal grandfather",
    ("father", "mother"): "paternal grandmother",
    ("mother", "father"): "maternal grandfather",
    ("mother", "mother"): "maternal grandmother",
    ("father", "son"): "brother",
    ("father", "daughter"): "sister",
    ("mother", "son"): "brother",
    ("mother", "daughter"): "sister",
    ("son", "son"): "grandson",
    ("son", "daughter"): "granddaughter",
    ("daughter", "son"): "grandson",
    ("daughter", "daughter"): "granddaughter",
    # sibling chains
    ("brother", "son"): "nephew",
    ("brother", "daughter"): "niece",
    ("sister", "son"): "nephew",
    ("sister", "daughter"): "niece",
    ("son", "father"): "father",
    ("son", "mother"): "mother",
    ("daughter", "father"): "father",
    ("daughter", "mother"): "mother",
    # grandparent chains
    ("grandfather", "son"): "uncle",
    ("grandfather", "daughter"): "aunt",
    ("grandmother", "son"): "uncle",
    ("grandmother", "daughter"): "aunt",
    ("grandson", "father"): "son",
    ("grandson", "mother"): "son",
    ("granddaughter", "father"): "daughter",
    ("granddaughter", "mother"): "daughter",
    # grandchild + sibling → other grandchild
    ("grandson", "sister"): "granddaughter",
    ("grandson", "brother"): "grandson",
    ("granddaughter", "sister"): "granddaughter",
    ("granddaughter", "brother"): "grandson",
    # grandparent + sibling → great-aunt/uncle (simplified)
    ("grandfather", "brother"): "great-uncle",
    ("grandfather", "sister"): "great-aunt",
    ("grandmother", "brother"): "great-uncle",
    ("grandmother", "sister"): "great-aunt",
    # sibling + grandchild → grandnephew/grandniece (simplified as nephew/niece)
    ("brother", "grandson"): "nephew",
    ("brother", "granddaughter"): "niece",
    ("sister", "grandson"): "nephew",
    ("sister", "granddaughter"): "niece",
    # uncle/aunt
    ("uncle", "son"): "cousin",
    ("uncle", "daughter"): "cousin",
    ("aunt", "son"): "cousin",
    ("aunt", "daughter"): "cousin",
    # spouse
    ("husband", "son"): "son",
    ("husband", "daughter"): "daughter",
    ("wife", "son"): "son",
    ("wife", "daughter"): "daughter",
    # in-laws
    ("father", "wife"): "mother",
    ("mother", "husband"): "father",
    ("son", "wife"): "daughter-in-law",
    ("daughter", "husband"): "son-in-law",
    ("husband", "father"): "father-in-law",
    ("husband", "mother"): "mother-in-law",
    ("wife", "father"): "father-in-law",
    ("wife", "mother"): "mother-in-law",
}

# Inverse relations
INVERSE: dict[str, str] = {
    "father": "son",
    "mother": "daughter",
    "son": "father",
    "daughter": "mother",
    "brother": "brother",
    "sister": "sister",
    "grandfather": "grandson",
    "grandmother": "granddaughter",
    "grandson": "grandfather",
    "granddaughter": "grandmother",
    "uncle": "nephew",
    "aunt": "niece",
    "nephew": "uncle",
    "niece": "aunt",
    "husband": "wife",
    "wife": "husband",
    "cousin": "cousin",
}

# Normalize output labels to match dataset gold labels
LABEL_NORMALIZE: dict[str, str] = {
    "paternal grandfather": "grandfather",
    "maternal grandfather": "grandfather",
    "paternal grandmother": "grandmother",
    "maternal grandmother": "grandmother",
    "daughter-in-law": "daughter-in-law",
    "son-in-law": "son-in-law",
    "father-in-law": "father-in-law",
    "mother-in-law": "mother-in-law",
}


_KNOWN_RELATIONS = {
    "father", "mother", "son", "daughter", "brother", "sister",
    "grandfather", "grandmother", "grandson", "granddaughter",
    "uncle", "aunt", "nephew", "niece", "husband", "wife", "cousin",
    "father-in-law", "mother-in-law", "son-in-law", "daughter-in-law",
    "great-uncle", "great-aunt",
}


def _norm_relation(rel: str) -> str:
    rel = rel.strip().lower().replace("-", " ").replace("_", " ")
    return rel


def _clean_relation(rel: str) -> str:
    """Normalize and de-pluralize relation strings."""
    rel = rel.strip().lower()
    # Remove possessive/plural trailing 's' if stemmed form is known
    if rel not in _KNOWN_RELATIONS and rel.endswith("s"):
        stemmed = rel[:-1]
        if stemmed in _KNOWN_RELATIONS:
            rel = stemmed
    return rel


def extract_entities_and_relations(story: str) -> dict[tuple[str, str], list[str]]:
    """
    Extract kinship relations from CLUTRR story text.
    Returns dict: (entity_a, entity_b) → [relation_from_a_to_b, ...]
    """
    relations: dict[tuple[str, str], list[str]] = {}

    # Pattern: [Name]'s [relation], [Name2]
    pattern1 = re.compile(
        r"\[([A-Za-z]+)\]'s\s+([\w\s\-]+?),?\s+\[([A-Za-z]+)\]", re.IGNORECASE
    )
    for m in pattern1.finditer(story):
        a, rel, b = m.group(1), _clean_relation(m.group(2)), m.group(3)
        key = (a, b)
        relations.setdefault(key, [])
        if rel not in relations[key]:
            relations[key].append(rel)
        # Add inverse
        inv_rel = INVERSE.get(rel)
        if inv_rel:
            inv_key = (b, a)
            relations.setdefault(inv_key, [])
            if inv_rel not in relations[inv_key]:
                relations[inv_key].append(inv_rel)

    # Pattern: [Name] likes to visit his/her [relation]. Her/His name is [Name2]
    pattern2 = re.compile(
        r"\[([A-Za-z]+)\][^\[.]*?(?:his|her)\s+([\w\s\-]+)\.\s+(?:Her|His) name is \[([A-Za-z]+)\]",
        re.IGNORECASE,
    )
    for m in pattern2.finditer(story):
        a, rel, b = m.group(1), _clean_relation(m.group(2)), m.group(3)
        key = (a, b)
        relations.setdefault(key, [])
        if rel not in relations[key]:
            relations[key].append(rel)
        inv_rel = INVERSE.get(rel)
        if inv_rel:
            inv_key = (b, a)
            relations.setdefault(inv_key, [])
            if inv_rel not in relations[inv_key]:
                relations[inv_key].append(inv_rel)

    # Pattern: [Name] ... her/his [relation], [Name2] — no other [Name] in between
    pattern_possessive = re.compile(
        r"\[([A-Za-z]+)\][^\[.]*?(?:his|her)\s+([\w\s\-]+?),\s*\[([A-Za-z]+)\]",
        re.IGNORECASE,
    )
    for m in pattern_possessive.finditer(story):
        a, rel, b = m.group(1), _clean_relation(m.group(2)), m.group(3)
        rel = re.sub(r"\s+", " ", rel).strip()
        if len(rel.split()) > 3:
            continue  # skip junk matches
        key = (a, b)
        relations.setdefault(key, [])
        if rel not in relations[key]:
            relations[key].append(rel)
        inv_rel = INVERSE.get(rel)
        if inv_rel:
            inv_key = (b, a)
            relations.setdefault(inv_key, [])
            if inv_rel not in relations[inv_key]:
                relations[inv_key].append(inv_rel)

    # Pattern: "The [relation]'s name is [Name]" paired with a nearby subject [SubjectName]
    # e.g. "[Clarence] has 3 children, and one grandson. The Grandson's name is [Tony]"
    # Find subject as last [Name] before "The [rel]'s name is"
    pattern_the_rel = re.compile(
        r"\[([A-Za-z]+)\][^.]*\.\s*The\s+([\w\s\-]+?)(?:'s)? name is \[([A-Za-z]+)\]",
        re.IGNORECASE,
    )
    for m in pattern_the_rel.finditer(story):
        a, rel, b = m.group(1), _clean_relation(m.group(2)), m.group(3)
        rel = re.sub(r"\s+", " ", rel).strip()
        key = (a, b)
        relations.setdefault(key, [])
        if rel not in relations[key]:
            relations[key].append(rel)
        inv_rel = INVERSE.get(rel)
        if inv_rel:
            inv_key = (b, a)
            relations.setdefault(inv_key, [])
            if inv_rel not in relations[inv_key]:
                relations[inv_key].append(inv_rel)

    # Pattern: [Name] and her/his [relation] [Name2]
    pattern3 = re.compile(
        r"\[([A-Za-z]+)\]\s+and\s+(?:her|his)\s+([\w\s\-]+?)\s+\[([A-Za-z]+)\]",
        re.IGNORECASE,
    )
    for m in pattern3.finditer(story):
        a, rel, b = m.group(1), _clean_relation(m.group(2)), m.group(3)
        key = (a, b)
        relations.setdefault(key, [])
        if rel not in relations[key]:
            relations[key].append(rel)
        inv_rel = INVERSE.get(rel)
        if inv_rel:
            inv_key = (b, a)
            relations.setdefault(inv_key, [])
            if inv_rel not in relations[inv_key]:
                relations[inv_key].append(inv_rel)

    return relations


def compose_relations(
    graph: dict[tuple[str, str], list[str]],
    source: str,
    target: str,
    max_hops: int = 6,
) -> str | None:
    """BFS over kinship graph to find relation from source to target."""
    from collections import deque

    queue: deque[tuple[str, str | None]] = deque()
    queue.append((source, None))
    visited: set[str] = {source}

    # BFS with relation tracking
    # State: (current_node, accumulated_relation)
    queue2: deque[tuple[str, str | None]] = deque()
    queue2.append((source, None))
    visited2: dict[str, str | None] = {source: None}

    while queue2:
        node, rel_so_far = queue2.popleft()
        if node == target:
            return rel_so_far

        for (a, b), rels in graph.items():
            if a != node:
                continue
            if b in visited2:
                continue
            for rel in rels:
                if rel_so_far is None:
                    new_rel = rel
                else:
                    new_rel = KINSHIP_COMPOSITION.get(
                        (_norm_relation(rel_so_far), _norm_relation(rel))
                    )
                    if new_rel is None:
                        new_rel = f"{rel_so_far}-{rel}"
                visited2[b] = new_rel
                queue2.append((b, new_rel))

    return None


def parse_proof_state(proof_state_str: str) -> dict | None:
    """Parse CLUTRR proof_state string (Python literal) safely."""
    try:
        return ast.literal_eval(proof_state_str)
    except Exception:
        return None


def solve_clutrr(
    story: str,
    query_str: str,
    proof_state_str: str = "",
) -> dict[str, Any]:
    """
    Solve CLUTRR kinship query using graph traversal.
    Returns: {answer, method, hops, failure_type, failure_detail, relations_found}
    """
    # Parse query: "('Alice', 'Bob')" → (Alice, Bob)
    query_str = query_str.strip()
    m = re.match(r"\('([^']+)',\s*'([^']+)'\)", query_str)
    if not m:
        return {
            "answer": "unknown",
            "method": "parse_error",
            "hops": 0,
            "failure_type": 2,
            "failure_detail": f"Cannot parse query: {query_str}",
            "relations_found": {},
        }

    source, target = m.group(1), m.group(2)
    relations = extract_entities_and_relations(story)

    answer = compose_relations(relations, source, target)
    if answer is not None:
        answer = LABEL_NORMALIZE.get(answer, answer)
        return {
            "answer": answer,
            "method": "graph_traversal",
            "hops": len(answer.split("-")) if "-" in answer else 1,
            "failure_type": 0,
            "failure_detail": "",
            "relations_found": {str(k): v for k, v in relations.items()},
        }

    # Failed — classify why
    entities_in_story = set()
    for a, b in relations.keys():
        entities_in_story.add(a)
        entities_in_story.add(b)

    if source not in entities_in_story:
        failure_type, detail = 1, f"Source entity '{source}' not found in story"
    elif target not in entities_in_story:
        failure_type, detail = 1, f"Target entity '{target}' not found in story"
    else:
        failure_type, detail = 3, (
            f"No path from '{source}' to '{target}' in kinship graph. "
            f"Entities found: {sorted(entities_in_story)}"
        )

    return {
        "answer": "unknown",
        "method": "graph_traversal_failed",
        "hops": 0,
        "failure_type": failure_type,
        "failure_detail": detail,
        "relations_found": {str(k): v for k, v in relations.items()},
    }


def extract_story_and_query(input_text: str) -> tuple[str, str]:
    """Extract Story: and Query: from CLUTRR input."""
    story = ""
    query = ""
    if "Story:" in input_text:
        parts = input_text.split("Story:", 1)[1]
        if "Query:" in parts:
            story_part, query_part = parts.split("Query:", 1)
            story = story_part.strip()
            query = query_part.strip()
        else:
            story = parts.strip()
    elif "Query:" in input_text:
        query = input_text.split("Query:", 1)[1].strip()
    return story, query
