"""SentBERT predicate similarity scorer with lazy loading and caching."""

from __future__ import annotations

from loguru import logger


class SentBERTSim:
    def __init__(self, model_name: str = "all-MiniLM-L6-v2", threshold: float = 0.75):
        self.model_name = model_name
        self.threshold = threshold
        self._model = None
        self._embed_cache: dict[str, list] = {}

    def _load(self):
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
                logger.info(f"Loading SentBERT model: {self.model_name}")
                self._model = SentenceTransformer(self.model_name)
                logger.info("SentBERT model loaded")
            except Exception as e:
                logger.warning(f"SentBERT unavailable: {e} — using string overlap fallback")
                self._model = "fallback"

    def _embed(self, text: str):
        if text in self._embed_cache:
            return self._embed_cache[text]
        if self._model == "fallback":
            return None
        import numpy as np
        emb = self._model.encode([text], show_progress_bar=False)[0]
        self._embed_cache[text] = emb
        return emb

    def similarity(self, a: str, b: str) -> float:
        self._load()
        if self._model == "fallback":
            return _string_overlap(a, b)
        import numpy as np
        ea, eb = self._embed(a), self._embed(b)
        if ea is None or eb is None:
            return _string_overlap(a, b)
        norm_a = np.linalg.norm(ea)
        norm_b = np.linalg.norm(eb)
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return float(np.dot(ea, eb) / (norm_a * norm_b))

    def find_similar(
        self, query: str, candidates: list[str], top_k: int = 3
    ) -> list[tuple[str, float]]:
        """Find top-k candidates similar to query above threshold."""
        if not candidates:
            return []
        self._load()
        scores = [(c, self.similarity(query, c)) for c in candidates]
        scores.sort(key=lambda x: x[1], reverse=True)
        return [(c, s) for c, s in scores[:top_k] if s >= self.threshold]

    def threshold_analysis(
        self, pairs: list[tuple[str, str, bool]], thresholds: list[float]
    ) -> dict[float, dict]:
        """
        Compute precision/recall at multiple thresholds.
        pairs: [(pred_a, pred_b, is_match), ...]
        """
        results = {}
        for thresh in thresholds:
            tp = fp = tn = fn = 0
            for a, b, label in pairs:
                sim = self.similarity(a, b)
                predicted = sim >= thresh
                if predicted and label:
                    tp += 1
                elif predicted and not label:
                    fp += 1
                elif not predicted and not label:
                    tn += 1
                else:
                    fn += 1
            precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
            recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
            f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
            results[thresh] = {
                "precision": round(precision, 4),
                "recall": round(recall, 4),
                "f1": round(f1, 4),
                "tp": tp, "fp": fp, "tn": tn, "fn": fn,
            }
        return results


def _string_overlap(a: str, b: str) -> float:
    """Fallback: normalized character bigram overlap."""
    a_set = set(zip(a, a[1:]))
    b_set = set(zip(b, b[1:]))
    if not a_set or not b_set:
        return 1.0 if a == b else 0.0
    return len(a_set & b_set) / len(a_set | b_set)
