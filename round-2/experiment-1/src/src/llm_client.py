"""OpenRouter LLM client with cost tracking and caching."""

import json
import os
import time
from pathlib import Path
from typing import Any

import requests
from loguru import logger

# Pricing per million tokens (input/output) for claude-haiku-4-5
PRICING = {
    "anthropic/claude-haiku-4-5": (0.80, 4.00),
    "anthropic/claude-haiku-4.5": (0.80, 4.00),
}
DEFAULT_PRICING = (1.00, 5.00)

API_URL = "https://openrouter.ai/api/v1/chat/completions"


class LLMClient:
    def __init__(self, model: str, cost_budget_usd: float = 10.0):
        self.model = model
        self.cost_budget_usd = cost_budget_usd
        self.total_cost_usd = 0.0
        self.total_calls = 0
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self._cache: dict[str, str] = {}
        self.api_key = os.environ.get("OPENROUTER_API_KEY", "")
        if not self.api_key:
            raise RuntimeError("OPENROUTER_API_KEY not set")
        pricing = PRICING.get(model, DEFAULT_PRICING)
        self.price_in_per_m = pricing[0]
        self.price_out_per_m = pricing[1]

    def call(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = 512,
        temperature: float = 0.0,
        cache_key: str | None = None,
    ) -> str:
        if self.total_cost_usd >= self.cost_budget_usd:
            raise RuntimeError(f"LLM cost budget exceeded: ${self.total_cost_usd:.4f}")

        if cache_key and cache_key in self._cache:
            logger.debug(f"LLM cache HIT for key: {cache_key[:60]}")
            return self._cache[cache_key]

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        for attempt in range(3):
            try:
                resp = requests.post(API_URL, json=payload, headers=headers, timeout=60)
                resp.raise_for_status()
                data = resp.json()
                text = data["choices"][0]["message"]["content"].strip()
                usage = data.get("usage", {})
                in_tok = usage.get("prompt_tokens", 0)
                out_tok = usage.get("completion_tokens", 0)
                cost = (in_tok * self.price_in_per_m + out_tok * self.price_out_per_m) / 1_000_000
                self.total_cost_usd += cost
                self.total_calls += 1
                self.total_input_tokens += in_tok
                self.total_output_tokens += out_tok
                logger.debug(
                    f"LLM call #{self.total_calls} | {in_tok}in/{out_tok}out | "
                    f"${cost:.4f} | cumulative: ${self.total_cost_usd:.4f}"
                )
                logger.debug(f"LLM response: {text[:200]}")
                if cache_key:
                    self._cache[cache_key] = text
                return text
            except requests.RequestException as e:
                logger.warning(f"LLM attempt {attempt+1} failed: {e}")
                if attempt < 2:
                    time.sleep(2 ** attempt)
        raise RuntimeError("LLM call failed after 3 attempts")

    def cost_summary(self) -> dict[str, Any]:
        return {
            "total_usd": round(self.total_cost_usd, 6),
            "total_calls": self.total_calls,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "model": self.model,
            "budget_usd": self.cost_budget_usd,
            "budget_remaining_usd": round(self.cost_budget_usd - self.total_cost_usd, 6),
        }
