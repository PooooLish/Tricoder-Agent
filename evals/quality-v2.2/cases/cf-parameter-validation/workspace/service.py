from validator import valid_quantity

def accept(value: int) -> str:
    return "ok" if valid_quantity(value) else "invalid"
