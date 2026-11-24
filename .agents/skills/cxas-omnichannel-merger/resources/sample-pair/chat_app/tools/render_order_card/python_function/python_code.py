"""Chat-only order card renderer tool."""


def render_order_card(order_number: str) -> dict:
    """Return a rich UI card payload for text clients."""
    return {"success": True, "type": "ORDER_CARD", "order_number": order_number}
