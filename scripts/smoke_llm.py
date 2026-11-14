"""Smoke test: one live agent call against the configured OpenAI-compatible endpoint."""

import asyncio

from app import workflow
from app.config import settings

conf = settings.llm_for("analysts")
print("endpoint:", conf["base_url"], "| model:", conf["model"])


async def _run() -> None:
    from agents import Agent, Runner

    llm = workflow.get_llm("analysts")
    if llm is None:
        raise SystemExit("no LLM configured (LLM_API_KEY empty?) - run uv run setup")
    agent = Agent(
        name="Connectivity Probe",
        instructions=(
            "You are a connectivity probe. Reply with only what the user asks for."
        ),
        model=llm.model_obj,
        model_settings=llm.model_settings,
    )
    result = await Runner.run(agent, 'Reply with only this JSON: {"ok": true}')
    usage = result.context_wrapper.usage
    print("response:", str(result.final_output or "")[:300])
    print(
        "usage: prompt=%d completion=%d total=%d"
        % (usage.input_tokens, usage.output_tokens, usage.total_tokens)
    )


asyncio.run(_run())
