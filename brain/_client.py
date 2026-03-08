"""
Shared Anthropic client singleton.

Lazy-initialised on first use so load_dotenv() has run before the API key is read.
Import _get_client() anywhere inside the brain package — never instantiate directly.
"""

import os

import anthropic

_anthropic_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    """Return the shared Anthropic client, creating it on first call."""
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _anthropic_client
