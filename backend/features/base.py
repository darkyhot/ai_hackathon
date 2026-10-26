"""Значение признака: что поставщик возвращает для записи в listing_features."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Value:
    key: str
    value: float | str | bool | None
    source: str
    confidence: float = 1.0
    evidence: str | None = None

    def columns(self) -> dict:
        v = self.value
        return {
            "value_bool": v if isinstance(v, bool) else None,
            "value_num": float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None,
            "value_text": v if isinstance(v, str) else None,
        }
