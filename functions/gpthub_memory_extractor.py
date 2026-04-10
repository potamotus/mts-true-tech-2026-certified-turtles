"""
GPTHub Memory Extractor — OpenWebUI Filter Function (outlet hook)

After each LLM response, buffers the conversation. When batch threshold
is reached or an explicit "запомни" trigger is detected, calls MWS GPT
to extract facts and stores them via the OpenWebUI Memories API.
"""

import time
import json
import re
import os

from memory.extraction import MemoryExtractor

# ── OpenWebUI Filter class ──────────────────────────────────────

class Filter:
    def __init__(self):
        self.type = "filter"
        self.id = "gpthub_memory_extractor"
        self.name = "GPTHub Memory Extractor"
        self.valves = {
            "confidence_threshold": 0.7,
            "batch_size": 5,
            "batch_timeout_sec": 300,
            "mws_gpt_endpoint": "https://api.gpt.mws.ru/v1",
            "extraction_model": "mws-gpt-alpha",
            "api_key": os.environ.get("MWS_API_KEY", ""),
        }
        self._extractor = None

    def _get_extractor(self):
        if self._extractor is None:
            self._extractor = MemoryExtractor(self.valves)
        return self._extractor

    # ── Helpers ─────────────────────────────────────────────────

    def _get_last_user_message(self, body: dict) -> str:
        messages = body.get("messages", [])
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, list):
                    # Multi-modal: extract text parts
                    parts = [p.get("text", "") for p in content if isinstance(p, dict)]
                    return " ".join(parts)
                return content
        return ""

    def _get_base_url(self) -> str:
        """Derive OpenWebUI base URL from environment or default."""
        return os.environ.get("WEBUI_URL", "http://localhost:3000")

    # ── Outlet hook ─────────────────────────────────────────────

    async def outlet(self, body: dict, user: dict, response: dict, __event_emitter__):
        """After each LLM response, buffer or extract memories."""
        try:
            extractor = self._get_extractor()
            user_id = user.get("id", "")
            chat_id = body.get("chat_id", "")
            messages = body.get("messages", [])
            last_user_msg = self._get_last_user_message(body)

            # Buffer this message
            extractor.buffer_message(user_id, chat_id, messages, response)

            # Check for explicit trigger — bypass batch
            if extractor.is_explicit_trigger(last_user_msg):
                await extractor.extract_and_store(
                    user_id=user_id,
                    chat_id=chat_id,
                    messages=messages,
                    response=response,
                    user_token=user.get("token", ""),
                    base_url=self._get_base_url(),
                    force=True,
                )
                return response

            # Check if batch is ready
            if extractor.should_extract(user_id):
                await extractor.extract_and_store(
                    user_id=user_id,
                    chat_id=chat_id,
                    messages=messages,
                    response=response,
                    user_token=user.get("token", ""),
                    base_url=self._get_base_url(),
                    force=False,
                )

        except Exception:
            # Graceful degradation — never crash the pipeline
            pass

        return response
