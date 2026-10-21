"""Tiny immutable ledger fixture used by a read-only review."""


def total(values: list[int]) -> int:
    return sum(values)
