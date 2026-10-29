"""Удобства, о которых просит клиент (балкон, санузел, гардеробная…), и что спросить у продавца.

Есть ли удобство — по полям площадки, описанию и словам продавца из звонка (x["confirmed"] — важнее всего).
"""
from __future__ import annotations

import re


# ключ: (как назвать, что искать в пожелании и описании, вопрос продавцу)
FEATURES = {
    "balcony": ("балкон или лоджия", r"балкон|лоджи", "есть ли балкон или лоджия"),
    "balcony_loggia": ("и балкон, и лоджия", r"балкон|лоджи", "есть ли и балкон, и лоджия"),
    "wardrobe": ("гардеробная", r"гардеробн", "есть ли гардеробная"),
    "storage": ("кладовая", r"кладов", "есть ли кладовая"),
    "parking": ("парковка", r"паркинг|парковк|машино-?мест", "есть ли парковка или машиноместо"),
    "separate_wc": ("раздельный санузел", r"раздельн\w*\s+(?:сан)?узел|(?:сан)?узел\w*\s+раздельн", "санузел раздельный или совмещённый"),
    "furniture": ("мебель", r"мебел", "остаётся ли мебель"),
}


FEATURE_WITH = {"balcony": "балконом или лоджией", "balcony_loggia": "балконом и лоджией", "wardrobe": "гардеробной", "storage": "кладовой",
                "parking": "парковкой", "separate_wc": "раздельным санузлом", "furniture": "мебелью"}


def merge_features(*lists: list[str]) -> list[str]:
    """Объединяет пожелания; «и балкон, и лоджия» уже включает «балкон или лоджия»."""
    out = list(dict.fromkeys(k for keys in lists for k in keys))
    return [k for k in out if not (k == "balcony" and "balcony_loggia" in out)]


def _outdoor_both(x: dict) -> bool | None:
    """Есть ли и балкон, и лоджия: по счётчикам площадки, иначе по описанию."""
    total, loggias = x.get("balconies"), x.get("loggias")
    if total is not None and loggias is not None and loggias > 0 and total - loggias > 0:
        return True
    text = (x.get("description") or "").lower()
    has = [re.search(p, text) and not re.search(rf"\bбез\s+(?:\w+\s+)?{p}", text) for p in ("балкон", "лоджи")]
    if all(has):
        return True
    if total is not None and loggias is not None:
        return False  # площадка указала количество — одного из двух нет
    return False if total == 0 else None


def feature_status(x: dict, key: str) -> bool | None:
    """True — есть по данным объявления, False — точно нет, None — не указано (уточним у продавца).
    Сказанное продавцом в звонке (x["confirmed"]) важнее объявления."""
    if (x.get("confirmed") or {}).get(key) is not None:
        return bool(x["confirmed"][key])
    if key == "balcony_loggia":
        return _outdoor_both(x)
    if key == "balcony" and x.get("balconies"):
        return True
    if key == "separate_wc" and x.get("separate_wc"):
        return True
    text = (x.get("description") or "").lower()
    pattern = FEATURES[key][1]
    if re.search(rf"\bбез\s+(?:\w+\s+)?(?:{pattern})", text):
        return False
    if re.search(pattern, text):
        return True
    if key == "balcony" and x.get("balconies") == 0:
        return False
    return None


MISSING_QUESTIONS = {
    "condition": ("состояние и ремонт", "в каком состоянии квартира, какой ремонт"),
    "year": ("год постройки", "какого года дом"),
    "kitchen": ("площадь кухни", "какая площадь кухни"),
    "floors": ("этажность дома", "сколько этажей в доме"),
}


ALWAYS_ASK = [("owners", "собственники", "сколько собственников и как давно квартира в собственности")]


NEW_BUILD_QUESTIONS = [
    ("deadline", "срок сдачи", "когда сдача дома и выдача ключей, были ли переносы"),
    ("condition", "отделка", "какая отделка — черновая, предчистовая или чистовая"),
    ("contract", "договор и эскроу", "по какому договору продаёте — ДДУ с эскроу, и есть ли скидка при ипотеке"),
]


def missing_info(listing: dict, limit: int = 3, features: list[str] | tuple = ()) -> list[dict]:
    """Что спросить у продавца: сначала удобства, о которых просил клиент, потом пробелы в объявлении.
    В новостройке — срок сдачи, отделка, договор (собственников и прописанных там нет)."""
    if listing.get("is_new"):
        out = [{"key": k, "label": FEATURES[k][0], "question": FEATURES[k][2]}
               for k in features if k in FEATURES and feature_status(listing, k) is None]
        out += [{"key": k, "label": lab, "question": q} for k, lab, q in NEW_BUILD_QUESTIONS
                if k == "contract" or listing.get(k) is None]
        return out[:limit]
    out = [{"key": k, "label": FEATURES[k][0], "question": FEATURES[k][2]}
           for k in features if k in FEATURES and feature_status(listing, k) is None]
    out += [{"key": k, "label": lab, "question": q} for k, (lab, q) in MISSING_QUESTIONS.items() if listing.get(k) is None]
    out += [{"key": k, "label": lab, "question": q} for k, lab, q in ALWAYS_ASK]
    return out[:limit]
