from decimal import Decimal

def invoice_total(lines):
    total = sum((line.amount for line in lines), Decimal("0.00"))
    return total.quantize(Decimal("0.01"))