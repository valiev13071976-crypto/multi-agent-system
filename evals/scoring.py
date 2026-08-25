"""Scoring helpers — binary first."""

from __future__ import annotations


def binary_score(passed: bool) -> float:
    return 1.0 if passed else 0.0


def clamp_score(value: float) -> float:
    score = float(value)
    if score < 0.0:
        return 0.0
    if score > 1.0:
        return 1.0
    return score
