"""Offline check: manager effort overrides global effort in actual requests."""

from unittest.mock import patch

from app import workflow
from app.config import settings


with patch.object(settings, "llm_reasoning_effort", "low"), patch.object(
    settings, "llm_reasoning_effort_manager", "high"
), patch("crewai.LLM", side_effect=lambda **kwargs: kwargs):
    for role, expected in (("manager", "high"), ("analysts", "low"), ("debate", "low")):
        request = workflow._build_llm(role)
        assert request["reasoning_effort"] == expected
        assert request["additional_params"]["reasoning_effort"] == expected
    with patch.object(settings, "llm_reasoning_effort_manager", ""):
        assert workflow._build_llm("manager")["reasoning_effort"] == "low"
    assert workflow._build_llm("manager", reasoning_effort="none")["reasoning_effort"] == "none"

print("MANAGER REASONING CHECKS PASSED")
