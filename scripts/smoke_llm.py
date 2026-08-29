"""Smoke test: CrewAI LLM against the configured OpenAI-compatible endpoint."""

from app.config import settings

model = settings.llm_model
if not model.startswith("openai/"):
    model = f"openai/{model}"

from crewai import LLM  # noqa: E402

print("methods:", [m for m in dir(LLM) if not m.startswith("_") and "call" in m.lower()])

llm = LLM(
    model=model,
    base_url=settings.llm_base_url,
    api_key=settings.llm_api_key,
    temperature=0.2,
    timeout=60,
)
response = llm.call(
    messages=[{"role": "user", "content": 'Reply with only this JSON: {"ok": true}'}]
)
print("response:", str(response)[:300])
