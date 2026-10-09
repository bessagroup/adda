"""ClaudeCodeAgent: a node whose session is plain Claude Code.

It gets the problem statement byte for byte as the first message, Claude Code's
own system prompt, its own full tool set, and nothing from adda: no notices, no
re-prompts, no wind-down. The backend ends the session when a set budget (wall
or tokens) is reached. Use it as the baseline arm of an ablation.
"""
from __future__ import annotations

from ..backends.base import DEFAULT_PROMPT, DEFAULT_TOOLS, Agent


class ClaudeCodeAgent(Agent):
    description = "Plain Claude Code: the statement in, nothing from adda."
    backend = "claude"
    system_prompt = ""
    base_prompt = DEFAULT_PROMPT
    tools = frozenset({DEFAULT_TOOLS})
