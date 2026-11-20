"""Read-only profit/loss amounts, returns and display colours."""

from decimal import Decimal, InvalidOperation

PROFIT_COLOURS = {"gain": "#b91c1c", "loss": "#1d4ed8", "neutral": "#303030"}


def finite(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except InvalidOperation:
        return None


def performance(value, basis):
    value, basis = finite(value), finite(basis)
    if value is None or basis is None or basis <= 0:
        return None, None
    net = value - basis
    return net, net / basis * 100


def profit_tag(net):
    number = finite(net)
    return "neutral" if number is None or number == 0 else "gain" if number > 0 else "loss"


def signed(number, places, *, trim=False):
    prefix = "+" if number > 0 else "-" if number < 0 else ""
    value = f"{abs(number):,.{places}f}"
    return prefix + (value.rstrip("0").rstrip(".") if trim else value)


def profit_display(net, percent):
    net, percent = finite(net), finite(percent)
    if net is None or percent is None:
        return "—"
    return f"{signed(net, 2, trim=True)}円（{signed(percent, 2)}%）"
