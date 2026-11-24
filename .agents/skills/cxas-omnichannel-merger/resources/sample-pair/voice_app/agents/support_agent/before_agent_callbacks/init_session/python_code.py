"""Turn-guarded before_agent_callback sample."""

from typing import Optional


def before_agent_callback(
    callback_context: CallbackContext,
) -> Optional[Content]:
    state = callback_context.state
    if state.get("_initialized") == "true":
        return None
    state["_initialized"] = "true"
    state.setdefault("order_id", "")
    return None
