from decimal import Decimal, InvalidOperation

TWO_PLACES = Decimal("0.01")
MAX_AMOUNT = Decimal("9999999999.99")  # matches DecimalField(max_digits=12, decimal_places=2)


def parse_money(text):
    """Parse a user-supplied amount like '500', '500.4', or '500.43'.
    Returns a Decimal quantized to 2 places, or None if invalid
    (non-numeric, zero/negative, more than 2 decimal places, or too large)."""
    try:
        amount = Decimal(str(text).strip().lstrip("$").replace(",", ""))
    except InvalidOperation:
        return None
    if not amount.is_finite() or amount <= 0 or amount > MAX_AMOUNT:
        return None
    if -amount.as_tuple().exponent > 2:
        return None
    return amount.quantize(TWO_PLACES)
