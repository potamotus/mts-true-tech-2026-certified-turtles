"""
GPTHub Memory Injector — OpenWebUI Filter Function (inlet hook)

Before each LLM request, retrieves relevant memories via smart
scoring (similarity + recency + category boost) and injects them
into the system prompt within a token budget.
"""

import os

from memory.retrieval import MemoryRetriever

# ── OpenWebUI Filter class ──────────────────────────────────────

class Filter:
    def __init__(self):
        self.type = "filter"
        self.id = "gpthub_memory_injector"
        self.name = "GPTHub Memory Injector"
        self.valves = {
            "max_memories": 5,
            "max_tokens": 2000,
            "category_weights": {
                "preference": 1.2,
                "deadline": 1.5,
                "project": 1.0,
                "decision": 1.0,
                "contact": 0.8,
                "skill": 0.8,
                "role": 0.8,
            },
        }
        self._retriever = None

    def _get_retriever(self):
        if self._retriever is None:
            self._retriever = MemoryRetriever(self.valves)
        return self._retriever

    # ── Helpers ─────────────────────────────────────────────────

    def _get_last_user_message(self, body: dict) -> str:
        messages = body.get("messages", [])
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, list):
                    parts = [p.get("text", "") for p in content if isinstance(p, dict)]
                    return " ".join(parts)
                return content
        return ""

    def _get_base_url(self) -> str:
        return os.environ.get("WEBUI_URL", "http://localhost:3000")

    def _inject_system_message(self, body: dict, system_prompt: str) -> dict:
        """Inject or prepend system message into the conversation."""
        messages = body.get("messages", [])

        # Check if there's already a system message
        has_system = any(m.get("role") == "system" for m in messages)

        if has_system:
            # Append memory context to existing system message
            for msg in messages:
                if msg.get("role") == "system":
                    existing = msg.get("content", "")
                    msg["content"] = f"{existing}\n\n{system_prompt}"
                    break
        else:
            # Insert system message at the beginning
            messages.insert(0, {"role": "system", "content": system_prompt})

        body["messages"] = messages
        return body

    # ── Inlet hook ──────────────────────────────────────────────

    async def inlet(self, body: dict, user: dict, __event_emitter__):
        """Before each LLM request, retrieve and inject memories."""
        try:
            retriever = self._get_retriever()
            user_id = user.get("id", "")
            last_user_msg = self._get_last_user_message(body)

            if not last_user_msg:
                return body

            # Detect query type for category boost
            query_type = retriever.detect_query_type(last_user_msg)

            # Retrieve with smart scoring
            memories = await retriever.retrieve(
                user_id=user_id,
                query=last_user_msg,
                user_token=user.get("token", ""),
                base_url=self._get_base_url(),
                query_type=query_type,
            )

            if not memories:
                return body

            # Assemble within token budget
            memory_context, _ = retriever.assemble_context(memories)

            if not memory_context:
                return body

            # Build injection prompt
            system_prompt = (
                "You are GPTHub — an AI assistant with long-term memory.\n\n"
                "RELEVANT CONTEXT ABOUT THIS USER:\n"
                f"{memory_context}\n\n"
                "Use this context to personalize responses. "
                "Reference known facts when relevant. "
                "If the user asks about something you have context on, use it."
            )

            body = self._inject_system_message(body, system_prompt)

        except Exception:
            # Graceful degradation — never crash the pipeline
            pass

        return body
