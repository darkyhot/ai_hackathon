"""Справочники, которые должны быть в базе всегда: каталог признаков."""
from __future__ import annotations

from sqlalchemy.orm import Session

from .models import Feature


def features_catalog(db: Session) -> None:
    from ..features import catalog

    known = {f.key: f for f in db.query(Feature).all()}
    for key, title, kind, unit, group, provider, implemented, description in catalog():
        f = known.get(key) or Feature(key=key)
        f.title, f.kind, f.unit, f.group, f.provider, f.implemented, f.description = (
            title, kind, unit, group, provider, implemented, description)
        db.add(f)
