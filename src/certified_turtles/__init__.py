"""Монолит GPTHub: API, оркестрация, клиент MWS GPT (пакет в src/)."""

from .mws_gpt.client import MWSGPTClient

__all__ = ["MWSGPTClient"]
