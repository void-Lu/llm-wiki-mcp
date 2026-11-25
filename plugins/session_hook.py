#!/usr/bin/env python3
"""Pure stdin/stdout event adapter; the builder embeds trusted static guidance."""

from __future__ import annotations

import json
import sys
from typing import Any, Sequence

HOSTS = {"codex", "claude", "copilot", "antigravity"}
SESSION_EVENTS = {"sessionstart"}
SESSION_GUIDANCE: str | None = None  # build-time embed


def _event(payload: dict[str, Any]) -> str | None:
    for key in ("hook_event_name", "hookEventName", "event_name", "eventName", "event"):
        value = payload.get(key)
        if isinstance(value, str):
            return "".join(character for character in value.lower() if character.isalnum())
    return None


def render_output(
    host: str, payload: Any, guidance: str | None = None
) -> dict[str, Any]:
    """Render only a session-start context response for the selected host."""

    if host not in HOSTS or not isinstance(payload, dict):
        return {}
    event = _event(payload)
    if event is not None and event not in SESSION_EVENTS:
        return {}
    context = SESSION_GUIDANCE if guidance is None else guidance
    if not context:
        return {}
    if host in {"codex", "claude"}:
        return {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": context,
            }
        }
    if host == "copilot":
        return {"additionalContext": context}
    return {"injectSteps": [{"systemMessage": {"systemMessage": context}}]}


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1 or arguments[0] not in HOSTS:
        print("usage: session_hook.py {codex|claude|copilot|antigravity}", file=sys.stderr)
        return 2
    raw = sys.stdin.buffer.read()
    if not raw.strip():
        payload: Any = {}
    else:
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeError):
            payload = None
    response = render_output(arguments[0], payload)
    sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
