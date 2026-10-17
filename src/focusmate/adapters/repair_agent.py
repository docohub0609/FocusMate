"""Repair uses native function calls and exactly one HTTP attempt per request."""
from dataclasses import replace

from focusmate.adapters.tool_chat import DeepSeekToolChatProvider
from apps.api.deepseek_provider import DeepSeekConfig


class DeepSeekRepairProvider(DeepSeekToolChatProvider):
    @classmethod
    def from_environment(cls):
        return cls(replace(DeepSeekConfig.from_environment(), max_tokens=8192))
