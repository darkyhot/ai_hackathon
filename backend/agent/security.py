"""Проверка безопасности объекта: что проверено, что нет и что агент закажет до сделки.

Реестры (ЕГРН, ФССП, ЕФРСБ, ЕИСЖС…) в прототипе не подключены, поэтому агент
никогда не пишет «чисто» по ним: каждая такая проверка — «не проверено», пока нет выписки.
Что можно сказать по объявлению — говорим; тревожные слова в описании поднимают
нужную проверку наверх и объясняют, почему она важна именно для этого объекта.
"""
from __future__ import annotations

import re

# id, что проверяем, где, что это покажет
SECONDARY = [
    ("egrn", "Выписка ЕГРН", "Росреестр", "кто собственник, доли, обременения: ипотека, арест, рента"),
    ("history", "История переходов права", "Росреестр", "частые перепродажи, наследство и дарение за последние 3 года"),
    ("seller_debts", "Долги продавца", "ФССП", "исполнительные производства — из-за них квартиру могут арестовать до сделки"),
    ("bankruptcy", "Банкротство продавца", "ЕФРСБ (Федресурс)", "если продавец обанкротится в течение 3 лет, сделку могут оспорить"),
    ("courts", "Суды", "ГАС «Правосудие»", "споры о квартире, разделе имущества, наследстве"),
    ("passport", "Паспорт продавца", "МВД", "паспорт действителен — защита от подставного продавца"),
    ("children", "Доли детей и маткапитал", "Опека, Соцфонд", "если покупали с маткапиталом, у детей должны быть доли; продажа — с разрешением опеки"),
    ("registered", "Кто прописан", "Выписка из домовой книги", "все ли выпишутся до сделки; дети и отказники от приватизации — риск"),
    ("replanning", "Перепланировка", "Техплан + просмотр", "совпадает ли квартира с документами; неузаконенная мешает ипотеке"),
    ("poa", "Доверенность", "Реестр доверенностей ФНП", "если продаёт представитель — доверенность действует и не отозвана"),
]

NEW_BUILD = [
    ("permit", "Разрешение и проектная декларация", "ЕИСЖС (наш.дом.рф)", "дом строится законно, сроки и характеристики как в рекламе"),
    ("escrow", "Эскроу-счёт", "ЕИСЖС, банк", "деньги лежат в банке до сдачи дома и вернутся, если застройщик не достроит"),
    ("developer", "Застройщик", "ЕГРЮЛ, ЕФРСБ, суды", "нет банкротства и крупных исков; сколько домов сдано и вовремя ли"),
    ("deadline", "Сроки сдачи", "ЕИСЖС", "были ли переносы по этому дому и другим домам застройщика"),
    ("contract", "Договор", "ДДУ, Росреестр", "ДДУ по 214-ФЗ: срок передачи, отделка, неустойка; при уступке — вся цепочка"),
    ("commissioning", "Ввод в эксплуатацию", "ЕИСЖС, Росреестр", "для сданного дома: разрешение на ввод и право собственности"),
]

# слово в описании -> какую проверку поднять и почему
TRIGGERS = [
    (r"\bдол(я|и|ю|ей)\b", "egrn", "в описании упомянута доля"),
    (r"несовершеннолет|детск\w* дол|маткапитал|материнск", "children", "упомянуты дети или маткапитал"),
    (r"наследств", "history", "квартира получена по наследству"),
    (r"дарен", "history", "квартира получена по дарению"),
    (r"перепланиров", "replanning", "упомянута перепланировка"),
    (r"обремен|арест|залог", "seller_debts", "упомянуто обременение"),
    (r"доверенност", "poa", "продажа по доверенности"),
    (r"прописан|зарегистрирован", "registered", "упомянуты прописанные"),
    (r"переуступ|уступк", "contract", "переуступка прав"),
]


def _listing_checks(listing: dict) -> list[dict]:
    """Что видно по самому объявлению (это и есть «проверено» в прототипе)."""
    from ..sources import description_flags

    out = []

    def add(title, status, note):
        out.append({"title": title, "status": status, "note": note})

    rr = listing.get("rosreestr_checked")
    if rr:
        add("Собственник", "ok", "площадка сверила собственника с Росреестром")
    else:
        add("Собственник", "info", "площадка собственника не сверяла")
    if listing.get("is_apartments"):
        add("Апартаменты", "warn", "не жильё: нельзя прописаться, ипотека сложнее")
    if listing.get("is_auction"):
        add("Продажа с торгов", "warn", "особый порядок сделки — нужен юрист")
    if listing.get("is_new"):
        if listing.get("sale_type"):
            add("Тип договора", "ok" if "ДДУ" in listing["sale_type"] else "info", listing["sale_type"])
        if listing.get("deadline"):
            add("Срок сдачи", "ok" if listing.get("finished") else "info", listing["deadline"])
    for note in description_flags(listing.get("description")):
        add("Описание", "warn", note)
    if not any(c["status"] == "warn" for c in out):
        add("Описание", "ok", "тревожных слов нет")
    return out


def dossier(listing: dict) -> dict:
    """Досье по объекту: по объявлению + список проверок до сделки со статусами."""
    text = (listing.get("description") or "").lower()
    is_new = bool(listing.get("is_new"))
    base = NEW_BUILD if is_new else SECONDARY
    flagged: dict[str, str] = {}
    for pattern, key, why in TRIGGERS:
        if re.search(pattern, text):
            flagged.setdefault(key, why)
    if listing.get("seller_role", "").startswith("собственник (переуступка"):
        flagged.setdefault("contract", "продаёт не застройщик")

    items = []
    for key, title, source, shows in base:
        if key == "escrow" and listing.get("finished"):
            continue
        if key == "commissioning" and is_new and listing.get("finished") is False:
            continue
        if key == "poa" and "poa" not in flagged:
            continue  # только если есть повод
        status, note = "not_checked", None
        if key == "egrn" and listing.get("rosreestr_checked"):
            status, note = "partial", "площадка сверила собственника; полную выписку всё равно закажу"
        items.append({"key": key, "title": title, "source": source, "shows": shows, "status": status,
                      "note": note, "priority": key in flagged, "why": flagged.get(key)})
    items.sort(key=lambda x: (not x["priority"], 0))

    checks = _listing_checks(listing)
    warn = any(c["status"] == "warn" for c in checks) or any(i["priority"] for i in items)
    done = sum(1 for i in items if i["status"] == "done")
    return {
        "listing_id": listing["id"],
        "is_new": is_new,
        "status": "warn" if warn else "ok",
        "checks": checks,
        "before_deal": items,
        "done": done,
        "total": len(items),
        "summary": (f"По объявлению {'есть оговорки' if warn else 'тревожных признаков нет'}. "
                    f"До сделки — {len(items)} {_plural(len(items))} по реестрам, пока не проверено ни одной."),
    }


def _plural(n: int) -> str:
    m = n % 100
    if 11 <= m <= 14:
        return "проверок"
    return {1: "проверка", 2: "проверки", 3: "проверки", 4: "проверки"}.get(n % 10, "проверок")
