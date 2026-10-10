"""Check that numbers in the agent's answer appear in tool outputs.

This is a guard against fabricated values. It cannot prove an interpretation
is right, but it flags any figure the model wrote that no tool returned, so the
user can see it was not verified.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Iterable

_NUMBER = re.compile(
    r"(?<![\w.])[-−]?(?:£|\$|€)?\s?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?\s?(%|k|m|bn|million|billion|thousand)?(?![\w])",
    re.I,
)
_SCALE = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "bn": 1e9, "billion": 1e9}


def _walk(value: Any) -> Iterable[float]:
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        if not (isinstance(value, float) and (math.isnan(value) or math.isinf(value))):
            yield float(value)
    elif isinstance(value, dict):
        for v in value.values():
            yield from _walk(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _walk(v)
    elif isinstance(value, str):
        for m in _NUMBER.finditer(value):
            try:
                yield float(m.group(1).replace(",", "") + (m.group(2) or ""))
            except ValueError:
                continue


def evidence_numbers(tool_outputs: list[str]) -> set[float]:
    numbers: set[float] = set()
    for text in tool_outputs:
        try:
            numbers.update(_walk(json.loads(text)))
        except (json.JSONDecodeError, TypeError):
            numbers.update(_walk(text))
    return numbers


def _matches(value: float, decimals: int, evidence: set[float]) -> bool:
    tol = 0.5 * 10 ** (-decimals) + 1e-9
    for e in evidence:
        for candidate in (e, e * 100):
            if abs(candidate - value) <= tol:
                return True
            if value and abs(candidate - value) / abs(value) < 0.005 and decimals == 0 and abs(value) >= 1000:
                return True
    return False


def unverified_numbers(answer: str, tool_outputs: list[str], user_text: str = "") -> list[str]:
    """Return numbers written in `answer` that do not appear in any tool output."""
    evidence = evidence_numbers(tool_outputs) | evidence_numbers([user_text])
    flagged: list[str] = []
    for m in _NUMBER.finditer(answer or ""):
        raw = m.group(0).strip()
        integer, frac, suffix = m.group(1), m.group(2) or "", (m.group(3) or "").lower()
        try:
            value = float(integer.replace(",", "") + frac)
        except ValueError:
            continue
        decimals = len(frac) - 1 if frac else 0
        if suffix in _SCALE:
            scaled = value * _SCALE[suffix]
            tol_rel = 0.5 * 10 ** (-decimals) / max(value, 1e-9)
            if any(abs(e - scaled) / max(abs(scaled), 1e-9) <= tol_rel + 1e-9 for e in evidence):
                continue
            flagged.append(raw)
            continue
        if not frac and not suffix and (value <= 12 or 1900 <= value <= 2100):
            continue  # small counts, list numbering, months and years
        if _matches(value, decimals, evidence):
            continue
        flagged.append(raw)
    seen: list[str] = []
    for item in flagged:
        if item not in seen:
            seen.append(item)
    return seen
