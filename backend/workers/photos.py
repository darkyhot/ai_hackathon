"""Копии фото квартир на диске (STORE_PHOTOS=true): объявление снимут — фото останутся для истории и сравнения.

Файл: PHOTOS_DIR/<listing_id>/<position>.jpg, в базе — путь и SHA-256 (одинаковые фото у разных
объявлений — признак дубля или перепродажи; пригодится команде персонализации).
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import httpx
from sqlalchemy import select

from .. import db
from ..config import settings
from ..db.models import Listing, ListingPhoto, utcnow

log = logging.getLogger(__name__)


async def download_missing(limit: int = 300) -> int:
    with db.session() as s:
        todo = s.execute(select(ListingPhoto.id, ListingPhoto.listing_id, ListingPhoto.position, ListingPhoto.url)
                         .join(Listing).where(ListingPhoto.storage_path.is_(None), Listing.status == "active")
                         .limit(limit)).all()
    done = 0
    base = Path(settings.photos_dir)
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as http:
        for pid, lid, pos, url in todo:
            try:
                r = await http.get(url)
                r.raise_for_status()
            except httpx.HTTPError as exc:
                log.info("фото %s не скачалось: %s", url, exc)
                continue
            path = base / lid / f"{pos}.jpg"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(r.content)
            with db.session() as s:
                p = s.get(ListingPhoto, pid)
                p.storage_path = str(path.relative_to(base))
                p.sha256 = hashlib.sha256(r.content).hexdigest()
                p.downloaded_at = utcnow()
            done += 1
    return done
