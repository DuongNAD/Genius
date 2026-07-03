import json
import logging
import sys
from typing import Any, Dict

# Centralized console logger configured to stdout
logger = logging.getLogger("ag_core")
logger.setLevel(logging.INFO)

# On Windows a redirected/piped stdout defaults to the locale code page
# (cp1252 & co.), and a log record with non-ASCII text (provider errors,
# masked git URLs, user prompts) would raise UnicodeEncodeError inside
# emit() — logging swallows it and the line is lost. Force UTF-8 with a
# lossless fallback; guarded because some replacement streams (test
# capture, custom redirects) don't support reconfigure().
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
except Exception:
    pass

# Prevent duplicate handlers
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)

# Model pricing structure (USD per 1,000,000 tokens)
MODEL_PRICING: Dict[str, Dict[str, float]] = {
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4": {"input": 30.00, "output": 60.00},
    "claude-3-5-sonnet": {"input": 3.00, "output": 15.00},
    "claude-3-5-haiku": {"input": 0.80, "output": 4.00},
    "grok-2": {"input": 2.00, "output": 10.00},
    "grok-beta": {"input": 5.00, "output": 15.00},
}
DEFAULT_PRICING = {"input": 2.50, "output": 10.00}  # Fallback rates


def calculate_usage_cost(
    model_name: str, prompt_tokens: int, completion_tokens: int
) -> float:
    """Calculates LLM query transaction cost based on token counts."""
    rates = DEFAULT_PRICING
    model_name_lower = model_name.lower()

    # Sort keys by length descending to match the most specific key first (e.g. gpt-4o-mini before gpt-4o)
    sorted_keys = sorted(MODEL_PRICING.keys(), key=len, reverse=True)
    for key in sorted_keys:
        if key in model_name_lower:
            rates = MODEL_PRICING[key]
            break

    input_cost = (prompt_tokens / 1_000_000) * rates["input"]
    output_cost = (completion_tokens / 1_000_000) * rates["output"]
    return input_cost + output_cost


def log_structured(
    event_type: str, data: Dict[str, Any], level: int = logging.INFO
) -> None:
    """Logs an event as a structured JSON string."""
    payload = {"event_type": event_type, "data": data}
    logger.log(level, json.dumps(payload))


def log_transaction(
    model_name: str, prompt_tokens: int, completion_tokens: int
) -> None:
    """Logs token consumption metrics and USD transaction costs."""
    cost = calculate_usage_cost(model_name, prompt_tokens, completion_tokens)
    total_tokens = prompt_tokens + completion_tokens

    logger.info("=" * 60)
    logger.info(f"LLM TRANSACTION METRICS | Model: {model_name}")
    logger.info(
        f"Tokens consumed: Prompt={prompt_tokens} | Completion={completion_tokens} | Total={total_tokens}"
    )
    logger.info(f"Estimated Cost: ${cost:.6f} USD")
    logger.info("=" * 60)

    # Write structured JSON transaction log
    log_structured(
        event_type="llm_transaction",
        data={
            "model_name": model_name,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "estimated_cost_usd": cost,
        },
    )
