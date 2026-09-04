"""Triage backed by an OpenAI model.

The shape of the answer is enforced by a JSON schema derived from TriageResult,
not by asking the model to "reply in JSON". That is the same technique already
used in the extractor's schema.py, and for the same reason: a schema the API
enforces cannot be ignored, whereas an instruction can.
"""

from __future__ import annotations

import os
import time
from typing import Optional

from ..changes import ChangeRequest
from ..triage import INSTRUCTIONS, TriageOutcome, TriageResult, build_context

# gpt-5 and o-series models think before answering, and that thinking is billed
# as output. Everything else rejects the parameter outright.
_REASONING_MODELS = ("gpt-5", "o1", "o3", "o4")


class TriageUnavailable(RuntimeError):
    """The model did not return a usable answer.

    Raised rather than guessed at. A change request that cannot be triaged goes
    to a person; it does not get a made-up classification.
    """


class OpenAITriager:
    name = "openai"

    def __init__(
        self,
        model: str = "gpt-5-mini",
        api_key: Optional[str] = None,
        effort: str = "low",
        max_output_tokens: int = 2000,
        timeout_s: float = 30.0,
    ) -> None:
        from openai import OpenAI

        key = api_key or os.environ.get("OPENAI_API_KEY")
        if not key:
            raise RuntimeError(
                "OPENAI_API_KEY is not set. Use the 'stub' provider for a run "
                "that does not call out to anything."
            )
        self._client = OpenAI(api_key=key, timeout=timeout_s)
        self.model = model
        self.effort = effort
        self.max_output_tokens = max_output_tokens

    def triage(self, record: dict, request: ChangeRequest) -> TriageOutcome:
        kwargs: dict = {
            "model": self.model,
            "instructions": INSTRUCTIONS,
            "input": build_context(record, request),
            "text_format": TriageResult,
            "max_output_tokens": self.max_output_tokens,
        }
        # Keep reasoning shallow. This is a bounded comparison of a handful of
        # numbers, not a problem that rewards a long chain of thought, and the
        # thinking tokens are the expensive part of the call.
        if self.model.startswith(_REASONING_MODELS):
            kwargs["reasoning"] = {"effort": self.effort}

        started = time.monotonic()
        response = self._client.responses.parse(**kwargs)
        latency_ms = int((time.monotonic() - started) * 1000)

        parsed = response.output_parsed
        if parsed is None:
            raise TriageUnavailable(
                f"no parsed output (status={getattr(response, 'status', '?')}, "
                f"incomplete={getattr(response, 'incomplete_details', None)})"
            )

        usage = response.usage
        return TriageOutcome(
            result=parsed,
            model=self.model,
            prompt_tokens=getattr(usage, "input_tokens", 0) or 0,
            completion_tokens=getattr(usage, "output_tokens", 0) or 0,
            latency_ms=latency_ms,
        )
