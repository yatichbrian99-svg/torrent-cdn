"""TCDN-AI Python SDK (OpenAI-compatible).

    from tcdn_ai_sdk import Client
    client = Client(api_key="tcdn_ai_...", base_url="https://api.torrentcdn.store")
    reply = client.chat.completions.create(
        model="llama-3.1-8b-instruct",
        messages=[{"role": "user", "content": "hi"}],
    )
    print(reply.choices[0].message.content)

Confidential tier: pass ``confidential=True`` **and** pin the coordinator keys via
``coordinator_public_keys={key_id: public_key}``. Without pinning, the client
refuses a confidential job rather than trusting a bundle on first use.
"""

from .client import (
    ChatCompletion,
    ChatCompletionChunk,
    Choice,
    Client,
    Delta,
    Message,
    Usage,
)
from .errors import (
    AttestationRequiredError,
    AuthenticationError,
    InsufficientCreditsError,
    InvalidRequestError,
    NoEligibleWorkerError,
    PartialResultError,
    RateLimitError,
    TCDNAIError,
    TicketVerificationError,
    WorkerUnavailableError,
)

__version__ = "0.1.0"

__all__ = [
    "AttestationRequiredError",
    "AuthenticationError",
    "ChatCompletion",
    "ChatCompletionChunk",
    "Choice",
    "Client",
    "Delta",
    "InsufficientCreditsError",
    "InvalidRequestError",
    "Message",
    "NoEligibleWorkerError",
    "PartialResultError",
    "RateLimitError",
    "TCDNAIError",
    "TicketVerificationError",
    "Usage",
    "WorkerUnavailableError",
    "__version__",
]
