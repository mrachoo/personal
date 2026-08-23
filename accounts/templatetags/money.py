from decimal import Decimal, InvalidOperation

from django import template

register = template.Library()


@register.filter
def usd(value):
    """Format a ledger amount as an unsigned dollar string: 1234.5 -> $1,234.50.
    Sign/color are handled by the calling template."""
    try:
        amount = abs(Decimal(str(value)))
    except (InvalidOperation, TypeError, ValueError):
        return value
    return f"${amount:,.2f}"
