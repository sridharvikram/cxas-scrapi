"""Voice-only DTMF prompt tool."""


def send_dtmf_prompt(prompt_text: str) -> dict:
    """Ask the telephony channel to gather keypad digits."""
    return {"success": True, "prompt": prompt_text}
