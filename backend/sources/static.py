"""Площадки, с которых у нас только сохранённые объявления (Авито, Яндекс Недвижимость).

Живого поиска нет: эти сайты закрыты от автоматических запросов. Объявления настоящие —
сохранены вручную через браузер (21.10.2026) и приведены к общему формату (см. base.py).
Агент показывает их наравне с остальными, а в отчёте по площадкам честно пишет «сохранённые».
Файлы: backend/data/demo/<площадка>/<город латиницей>_<комнаты>[_new].json, например avito/kazan_2.json
"""
from __future__ import annotations

import json
from pathlib import Path

from .base import SourceUnavailable

DATA = Path(__file__).resolve().parent.parent / "data" / "demo"
# имена файлов латиницей: кириллица в путях ломается на macOS и в git
CITY_KEYS = {"Москва": "moscow", "Санкт-Петербург": "spb", "Казань": "kazan", "Новосибирск": "novosibirsk",
             "Екатеринбург": "ekaterinburg", "Нижний Новгород": "nn", "Краснодар": "krasnodar"}


class StaticSource:
    def __init__(self, key: str, name: str):
        self.SOURCE = key
        self.SOURCE_NAME = name
        self.dir = DATA / key

    def _path(self, city: str, rooms: int, new: bool) -> Path:
        return self.dir / f"{CITY_KEYS.get(city, city)}_{rooms}{'_new' if new else ''}.json"

    def supported(self, city: str | None) -> bool:
        return bool(city) and any(self.dir.glob(f"{CITY_KEYS.get(city, city)}_*.json"))

    def load_snapshot(self, city: str, rooms: int, allow_demo: bool = True, new: bool = False) -> tuple[list[dict], str | None]:
        """Отдельный файл для новостроек, если есть; иначе общий срез площадки, поделённый по признаку is_new."""
        if not city:
            return [], None
        split = not self._path(city, rooms, True).exists()  # отдельного файла новостроек нет — делим общий
        path = self._path(city, rooms, False if split else new)
        if not path.exists():
            return [], None
        data = json.loads(path.read_text(encoding="utf-8"))
        listings = [x for x in data["listings"] if bool(x.get("is_new")) == new] if split else data["listings"]
        return listings, data.get("fetched_at")

    async def search(self, city: str, rooms: int, budget: int | None, page: int = 1, okrug: str | None = None,
                     new: bool = False) -> tuple[list[dict], dict]:
        if page > 1:
            raise SourceUnavailable(f"{self.SOURCE_NAME}: больше сохранённых объявлений нет")
        saved, fetched_at = self.load_snapshot(city, rooms, new=new)
        saved = [x for x in saved if (not budget or x["price"] <= budget * 1.05) and (not okrug or x.get("okrug") == okrug)]
        if not saved:
            raise SourceUnavailable(f"{self.SOURCE_NAME}: нет сохранённых объявлений для «{city}»"
                                    + (" (новостройки)" if new else ""))
        return saved, {"status": "static", "note": f"сохранённые объявления от {fetched_at}; живой поиск площадка закрывает"}


avito = StaticSource("avito", "Авито")
yandex = StaticSource("yandex", "Яндекс Недвижимость")
