from decimal import Decimal

def invoice_total(lines):
    return sum(line.amount for line in lines)