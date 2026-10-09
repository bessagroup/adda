"""Render the argv the real SDK builds for a plain Claude Code turn."""
import json

import claude_agent_sdk as sdk
from claude_agent_sdk._internal.transport.subprocess_cli import (
    SubprocessCLITransport,
)

import adda._src.backends.claude as cmod
from adda._src.backends.base import DEFAULT_PROMPT, DEFAULT_TOOLS

seen = {}


async def _q(prompt=None, options=None, **kw):
    t = SubprocessCLITransport(prompt=prompt, options=options)
    t._cli_path = "claude"
    seen["cmd"] = t._build_command()
    if False:
        yield


cmod.query = sdk.query = _q
a = cmod.ClaudeAdapter("claude-haiku-4-5-20251001", "", None, [DEFAULT_TOOLS])
a.base_prompt = DEFAULT_PROMPT
try:
    a.invoke([{"role": "user", "content": "x"}])
except Exception:
    pass
print(json.dumps(seen))
