"""
blockrun-litellm — LiteLLM adapter for BlockRun.

Two integration modes:

1. **Custom provider** (in-process):

    >>> import litellm
    >>> from blockrun_litellm import register
    >>> register()  # adds "blockrun/" provider to LiteLLM
    >>> resp = litellm.completion(
    ...     model="blockrun/openai/gpt-5.5",
    ...     messages=[{"role": "user", "content": "Hello"}],
    ... )

2. **Local OpenAI-compatible proxy** (sidecar):

    $ blockrun-litellm-proxy --port 4001
    # then point LiteLLM at http://localhost:4001/v1

The adapter delegates x402 wallet signing and payment to the
``blockrun-llm`` SDK; your private key never leaves the host.
"""

from blockrun_litellm.logger import enable_local_logging
from blockrun_litellm.provider import BlockRunLLM, register
from blockrun_litellm.catalog import (
    BLOCKRUN_MODEL_IDS,
    CATALOG_SNAPSHOT_DATE,
    is_known_model,
    model_ids,
)

__all__ = [
    "BLOCKRUN_MODEL_IDS",
    "CATALOG_SNAPSHOT_DATE",
    "BlockRunLLM",
    "enable_local_logging",
    "is_known_model",
    "model_ids",
    "register",
]
__version__ = "0.9.1"
