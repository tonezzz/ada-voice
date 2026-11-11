"""Ada expert-consultation tool — read-only advice from a stronger model.

Ada asks; a stronger model answers; Ada narrates. Consult is read-only advice
— never execution authority.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from google.genai import types

from backend import gemini_pool

logger = logging.getLogger("ada_consult")

DECLARATION = {
    "name": "ada_consult",
    "description": (
        "Consult an expert model for a second opinion on a hard or ambiguous problem. "
        "Returns a structured answer. The expert only sees the bounded question and "
        "context provided here; it has no access to the live conversation, memory, or "
        "tools. This is a read-only request for advice, not an action."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The specific question to ask the expert.",
            },
            "context": {
                "type": "string",
                "description": "All necessary context and constraints the expert needs to answer.",
            },
            "consult_model": {
                "type": "string",
                "description": "Optional specific model alias to consult (e.g., 'gemini-3.5-pro'). Defaults to standard consult tier.",
            },
        },
        "required": ["question", "context"],
    },
}

_DEFAULT_CONSULT_MODEL = "gemini-3.5-flash"


async def run(runner: Any, **args: Any) -> dict[str, Any]:
    question = str(args.get("question") or "").strip()
    context = str(args.get("context") or "").strip()
    if not question:
        return {"ok": False, "error": "question is required"}
    if not context:
        return {"ok": False, "error": "context is required"}

    model = str(args.get("consult_model") or "").strip()
    if not model:
        model = os.environ.get("ADA_CONSULT_MODEL", _DEFAULT_CONSULT_MODEL)

    max_tokens = int(os.environ.get("ADA_CONSULT_MAX_TOKENS", "4096"))
    # The max_usd and max_wall_s bounds are conceptually applicable but Litellm
    # handles them mostly or we enforce via timeout.

    # 1. Transport setup
    # If ADA_CONSULT_KEY is set, use it. Else fallback to standard rest_key.
    api_key = os.environ.get("LITELLM_KEY_CONSULT") or os.environ.get("ADA_GEMINI_REST_API_KEY")
    client = gemini_pool.rest_client(api_key=api_key)

    # 2. Ops Event: requested
    runner.context.emit_ops_event(
        ev_type="consult_requested",
        detail=f"Consult requested for model={model}, question={question[:100]}...",
        tool="ada_consult"
    )

    t0 = time.monotonic()

    prompt = f"Question: {question}\n\nContext:\n{context}\n\nPlease provide a clear, expert answer based solely on the context provided."

    try:
        # 3. Model call
        resp = await client.aio.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                max_output_tokens=max_tokens,
                temperature=0.4,
            ),
        )
        answer = resp.text

        # 4. Ops Event: completed
        latency_s = time.monotonic() - t0
        runner.context.emit_ops_event(
            ev_type="consult_completed",
            detail=f"Consult completed for model={model}, latency={latency_s:.2f}s",
            tool="ada_consult"
        )

        return {
            "ok": True,
            "model": model,
            "answer": answer,
        }

    except Exception as exc:
        latency_s = time.monotonic() - t0
        # 5. Ops Event: failed
        runner.context.emit_ops_event(
            ev_type="consult_failed",
            detail=f"Consult failed for model={model}, latency={latency_s:.2f}s: {exc}",
            tool="ada_consult"
        )
        return {"ok": False, "error": f"consultation failed: {exc}"}
