"""Estimated LLM cost for one analysis run (first slice of ROADMAP P2.2's
run metrics: token use + estimated model cost).

Cost is an estimate, never an invoice: it multiplies recorded token usage by
public list prices. Provider discounts, cached-input rates, batch pricing,
and gateway fees are ignored on purpose. A model without a known price is
reported as unpriced instead of guessing, so the UI can say `cost unknown`
rather than showing a confident wrong number.

Prices are USD per 1M tokens (input, output), checked 2026-09-10 against the
providers' own pricing pages:

- Z.ai GLM-5.3: $1.40 / $4.40 (unchanged from GLM-5.2)
- Z.ai GLM-5.3-Flash: $0.15 / $0.50 base list (a promotion halves it; the
  list price is the honest default)
- OpenAI gpt-5.6-luna: $0.20 / $1.20 (since 2026-07-30)
- OpenAI gpt-5.6-terra / gpt-5.6-sol: $2.00 / $12.00 and $5.00 / $30.00
- OpenAI gpt-4o-mini / gpt-4o: $0.15 / $0.60 and $2.50 / $10.00
- OpenAI gpt-4.1-mini / gpt-4.1: $0.40 / $1.60 and $2.00 / $8.00
"""

PRICES_AS_OF = "2026-09-10"

# Normalized model name -> (input USD per 1M tokens, output USD per 1M tokens).
# Keys go through normalize_model(): lowercase, part after the last "/",
# everything that is not a letter or digit dropped ("zai-org/GLM-5.3-Flash"
# and "glm-5.3-flash" both match "glm53flash").
MODEL_PRICES: dict[str, tuple[float, float]] = {
    "glm53": (1.40, 4.40),
    "glm53flash": (0.15, 0.50),
    "gpt56luna": (0.20, 1.20),
    "gpt56terra": (2.00, 12.00),
    "gpt56sol": (5.00, 30.00),
    "gpt4omini": (0.15, 0.60),
    "gpt4o": (2.50, 10.00),
    "gpt41mini": (0.40, 1.60),
    "gpt41": (2.00, 8.00),
}


def normalize_model(model: str) -> str:
    """Fold a configured model name onto its price-table key."""
    name = str(model or "").strip().lower()
    if "/" in name:
        name = name.rsplit("/", 1)[1]
    return "".join(ch for ch in name if ch.isalnum())


def price_for(model: str) -> tuple[float, float] | None:
    """List price for a model, or None when it is not in the table."""
    return MODEL_PRICES.get(normalize_model(model))


def estimate(
    by_role: dict[str, dict],
    price_in: float | None = None,
    price_out: float | None = None,
) -> dict:
    """Cost estimate from per-role usage recorded during one ticker's run.

    `by_role` maps a role (manager / analysts / debate) to the model it ran
    and its token counts. `price_in` / `price_out` (USD per 1M tokens) override
    the table for every model, for custom or proxied pricing. Returns {} when
    no role recorded tokens (mock mode, provider metrics missing). A role
    whose model has no price stays in the report as unpriced; `total_usd` is
    then a floor, and `priced` is False.
    """
    roles: dict[str, dict] = {}
    unpriced: list[str] = []
    total = 0.0
    for role in sorted(by_role):
        entry = by_role[role]
        prompt = int(entry.get("prompt_tokens", 0) or 0)
        completion = int(entry.get("completion_tokens", 0) or 0)
        model = str(entry.get("model") or "")
        if not (prompt or completion):
            continue
        rates = (
            (float(price_in), float(price_out))
            if price_in and price_out
            else price_for(model)
        )
        if rates is None:
            unpriced.append(model or role)
            usd = None
        else:
            # OpenAI-compatible usage counts reasoning tokens inside
            # completion_tokens, so billing completion only is correct.
            usd = round((prompt * rates[0] + completion * rates[1]) / 1_000_000, 6)
            total += usd
        roles[role] = {
            "model": model,
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "usd": usd,
        }
    if not roles:
        return {}
    return {
        "total_usd": round(total, 6),
        "priced": not unpriced,
        "by_role": roles,
        "unpriced_models": sorted(set(unpriced)),
        "prices_as_of": PRICES_AS_OF,
    }
