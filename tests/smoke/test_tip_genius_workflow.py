"""Live smoke tests for configured LLM providers.

Sends a tiny JSON-mode-safe prompt to every provider listed in
``tip_genius_config.yaml -> llm_provider_options`` and asserts that a
non-empty response comes back. Validates real API keys, endpoints, and
model identifiers end-to-end through the production ``LLMManager`` path.

Gated by the ``smoke`` pytest marker (deselected by default) AND the
``RUN_SMOKE=1`` environment variable, since each invocation costs money.

Run with::

    RUN_SMOKE=1 uv run pytest tests/smoke -m smoke -v
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml
from dotenv import load_dotenv
from lib.llm_manager import LLMManager

REPO_ROOT = Path(__file__).resolve().parents[2]
TIP_GENIUS_CONFIG = REPO_ROOT / "src" / "tip_genius" / "cfg" / "tip_genius_config.yaml"

# Minimal odds-style prompt aligned with the production system prompt.
# Using a generic prompt would conflict with the system instruction and cause
# reasoning models (e.g. GPT-OSS) to emit empty content.
SMOKE_PROMPT = "Liverpool FC : 2.1, Manchester United : 2.0, draw : 1.9"


def _active_providers() -> list[str]:
    with TIP_GENIUS_CONFIG.open(encoding="utf-8") as f:
        config = yaml.safe_load(f)
    return list(config["llm_provider_options"])


pytestmark = [
    pytest.mark.smoke,
    pytest.mark.skipif(
        os.environ.get("RUN_SMOKE") != "1",
        reason="Smoke tests incur API cost; set RUN_SMOKE=1 to enable.",
    ),
]


@pytest.fixture(scope="session", autouse=True)
def _load_env() -> None:
    load_dotenv(REPO_ROOT / ".env.local")


@pytest.mark.parametrize("provider", _active_providers())
def test_llm_provider_responds(provider: str) -> None:
    """Each active provider returns a non-empty response to a trivial prompt."""
    llm = LLMManager(provider=provider)
    response = llm.get_prediction(SMOKE_PROMPT, timeout=60)
    assert isinstance(response, str)
    assert response.strip(), f"{provider} returned empty response"
