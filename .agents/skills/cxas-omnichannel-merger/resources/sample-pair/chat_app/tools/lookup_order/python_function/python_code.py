"""Look up order details."""


def lookup_order(order_number: str) -> dict:
    """Return sample status details for an order number."""
    return {"success": True, "order_number": order_number, "status": "Shipped"}
