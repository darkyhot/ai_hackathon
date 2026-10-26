"""Речевая аналитика звонка: какие факты сообщил продавец и насколько им можно верить.

Продавец мог ошибиться, забыть, приукрасить или передумать. Поэтому у каждого факта —
цитата-доказательство и уверенность 0..1 (никогда не 1: слова — не документ), а также причины:
  - оговорки («вроде», «наверное», «не помню», «уточню») — уверенность ниже;
  - расхождение с объявлением (год, этажность, кухня, срок сдачи) — ниже и флаг «противоречие»;
  - продавец поменял ответ в ходе звонка (сначала одно число, потом другое) — ниже и флаг;
  - договорённость о цене — устная: до задатка или предварительного договора продавец может передумать;
    если в звонке продавец поднимал цену после уступки, уверенность ещё ниже.
Каждому факту — чем его подтвердить (выписка ЕГРН, техплан, наш.дом.рф, просмотр).

Всё считается правилами по расшифровке — без LLM: результат воспроизводим и объясним.
Сейчас расшифровка — текст (симулятор продавца или ответы человека); с телефонией (заглушка)
на вход придёт распознанная речь, а к признакам добавятся паузы и темп.
"""
from __future__ import annotations

import re

STRONG_HEDGES = r"не помню|не знаю|не уверен|не уверена|уточню|надо посмотреть|надо уточнить|забыл|забыла|сложно сказать"
WEAK_HEDGES = r"вроде|кажется|наверное|наверно|примерно|где-то|около|по-моему|если не ошибаюсь|скорее всего|приблизительно|плюс-минус"
CORRECTIONS = r"точнее|вернее|ой,? нет|нет,? подождите|поправлюсь|я ошиб"

TOPIC = {  # слова, по которым находим ответ продавца на вопрос
    "year": r"\bдом\b|построен|постройк",
    "kitchen": r"кухн",
    "floors": r"этаж",
    "owners": r"собственник|владе|в собственности",
    "condition": r"ремонт|состояни|отделк|чернов|чистов",
    "deadline": r"сдач|ключ|срок|квартал|кв\.",
    "contract": r"ДДУ|договор|уступк|эскроу|214",
    "residents": r"пропис|зарегистр",
}

VERIFY = {
    "owners": "выписка ЕГРН (собственники и дата права)",
    "year": "выписка ЕГРН и сведения о доме",
    "floors": "сведения о доме, просмотр",
    "kitchen": "техплан, замер на просмотре",
    "condition": "просмотр квартиры",
    "deadline": "проектная декларация на наш.дом.рф",
    "contract": "текст ДДУ и выписка ЕГРН по договору",
    "residents": "выписка из домовой книги",
    "price_agreement": "задаток или предварительный договор через СБР",
}

BASE = 0.8


def _stem_words(text: str) -> set[str]:
    return {w[:5] for w in re.findall(r"[а-яёa-z0-9]+", text.lower()) if len(w) > 2 or w.isdigit()}


def _numbers(text: str) -> list[float]:
    return [float(n.replace(",", ".")) for n in re.findall(r"\d+(?:[.,]\d+)?", text)]


def _quote(value: str, key: str, seller_turns: list[str]) -> tuple[str | None, int | None]:
    """Реплика продавца, где он это сказал: больше всего общих слов со значением, иначе — по теме вопроса."""
    want = _stem_words(value)
    best, best_i, best_score = None, None, 0
    for i, t in enumerate(seller_turns):
        score = len(want & _stem_words(t))
        if score > best_score:
            best, best_i, best_score = t, i, score
    if best is None and key in TOPIC:
        for i, t in enumerate(seller_turns):
            if re.search(TOPIC[key], t, re.I):
                return t, i
    return best, best_i


def _sentence(quote: str, value: str, key: str) -> str:
    """Короткая цитата: предложение из реплики, где сказано это значение (при равенстве — по теме вопроса)."""
    want = _stem_words(value)
    topic = TOPIC.get(key)
    parts = re.split(r"(?<=[.!?])\s+", quote)
    best = max(parts, key=lambda p: (len(want & _stem_words(p)), bool(topic and re.search(topic, p, re.I))))
    return best.strip()[:240]


def _hedge_scope(turn: str | None, short: str | None, key: str) -> str:
    """Где искать оговорки: предложение с ответом и следующее за ним («…с 2015 года. Точно не помню»),
    если следующее не про другую тему («…1998 года. Кухня вроде 9 метров» — «вроде» про кухню, не про год)."""
    if not turn or not short:
        return turn or ""
    i = turn.find(short)
    rest = re.split(r"(?<=[.!?])\s+", turn[i + len(short):].strip()) if i >= 0 else []
    nxt = rest[0] if rest and rest[0] else ""
    if nxt and any(re.search(t, nxt, re.I) for k, t in TOPIC.items() if k != key):
        nxt = ""
    return f"{short} {nxt}".strip()


def _contradiction(key: str, value: str, listing: dict) -> str | None:
    nums = [n for n in _numbers(value) if n < 10_000]
    if not nums:
        return None
    if key == "year" and listing.get("year"):
        y = next((n for n in nums if 1800 < n < 2100), None)
        if y and abs(y - listing["year"]) > 1:
            return f"в объявлении — {listing['year']} год"
    if key == "floors" and listing.get("floors"):
        if all(n != listing["floors"] for n in nums if n < 100):
            return f"в объявлении — {listing['floors']} этажей"
    if key == "kitchen" and listing.get("kitchen"):
        k = next((n for n in nums if 2 < n < 60), None)
        if k and abs(k - listing["kitchen"]) > 1:
            return f"в объявлении — кухня {listing['kitchen']:g} м²"
    if key == "deadline" and listing.get("deadline"):
        y_listing = _numbers(listing["deadline"])
        y_said = [n for n in nums if 2000 < n < 2100]
        if y_listing and y_said and max(y_listing) != y_said[0]:
            return f"в объявлении — {listing['deadline']}"
    return None


RANGE = {"year": (1800, 2100), "kitchen": (2, 60), "floors": (1, 100), "owners": (1, 10)}


def _changed(key: str, seller_turns: list[str]) -> bool:
    """Продавец назвал по этой теме разные числа в разных репликах или сам поправился."""
    if key not in TOPIC:
        return False
    said = []
    for t in seller_turns:
        for sent in re.split(r"(?<=[.!?])\s+", t):
            if re.search(TOPIC[key], sent, re.I):
                if re.search(CORRECTIONS, sent, re.I):
                    return True
                lo, hi = RANGE.get(key, (0, 10_000))
                nums = {n for n in _numbers(sent) if lo <= n <= hi}
                if nums:
                    said.append(nums)
    return len(said) > 1 and not set.intersection(*said)


def _level(c: float) -> str:
    return "high" if c >= 0.75 else "medium" if c >= 0.5 else "low"


def fact(key: str, label: str, value: str, seller_turns: list[str], listing: dict, mode: str) -> dict:
    quote, _ = _quote(value, key, seller_turns)
    short = _sentence(quote, value, key) if quote else None
    text = f"{_hedge_scope(quote, short, key)} {value}"
    reasons = ["продавец-симулятор (демо)" if mode == "auto" else "ответ продавца в звонке"]
    c = BASE
    hedged = False
    if re.search(STRONG_HEDGES, text, re.I):
        c -= 0.35
        hedged = True
        reasons.append("продавец не уверен («не помню», «уточню»)")
    elif re.search(WEAK_HEDGES, text, re.I):
        c -= 0.15
        hedged = True
        reasons.append("ответ приблизительный («вроде», «около»)")
    contra = _contradiction(key, value, listing)
    if contra:
        c -= 0.3
        reasons.append(f"расходится с объявлением: {contra}")
    changed = _changed(key, seller_turns)
    if changed:
        c -= 0.25
        reasons.append("в ходе звонка продавец называл разное")
    if not quote:
        c -= 0.2
        reasons.append("не нашёл эту фразу в расшифровке — факт извлечён моделью")
    c = round(max(0.1, min(0.95, c)), 2)
    nums = [n for n in _numbers(value) if n < 10_000]
    return {
        "key": key, "label": label, "value": value, "quote": short, "confidence": c, "level": _level(c),
        "hedged": hedged, "contradicts_listing": bool(contra), "changed_during_call": changed,
        "reasons": reasons, "verify_with": VERIFY.get(key, "документы и просмотр"),
        "value_num": nums[0] if key in ("year", "kitchen", "floors") and nums else None,
    }


def price_fact(history: list[dict], listing: dict, status: str, price: int) -> dict | None:
    if status != "agreed":
        return None
    asks = [h.get("ask") for h in history if h["speaker"] == "seller" and h.get("ask")]
    raised = any(b > a for a, b in zip(asks, asks[1:]))
    c, reasons = 0.6, ["устная договорённость: до задатка продавец может передумать"]
    if raised:
        c -= 0.15
        reasons.append("в звонке продавец поднимал цену после уступки")
    quote = next((h["text"] for h in reversed(history) if h["speaker"] == "seller"
                  and re.search(r"соглас|договорил|по рукам|млн|тысяч|\d{3}\s?\d{3}|\d[.,]\d", h["text"], re.I)), None)
    return {
        "key": "price_agreement", "label": "Договорённость о цене", "value": f"{price:,} ₽".replace(",", " "),
        "quote": (quote or "")[:240] or None, "confidence": round(c, 2), "level": _level(c), "hedged": False,
        "contradicts_listing": False, "changed_during_call": raised, "reasons": reasons,
        "verify_with": VERIFY["price_agreement"], "value_num": None,
    }


def analyze(history: list[dict], learned: dict[str, str], questions: list[dict], listing: dict, mode: str,
            status: str, price: int) -> tuple[list[dict], dict]:
    """Факты звонка с уверенностью и сводка: сколько оговорок, противоречий, смен ответа."""
    seller_turns = [h["text"] for h in history if h["speaker"] == "seller"]
    by_label = {q["label"]: q["key"] for q in questions}
    facts = [fact(by_label.get(label, re.sub(r"\W+", "_", label.lower())[:40]), label, str(value), seller_turns, listing, mode)
             for label, value in learned.items() if value]
    pf = price_fact(history, listing, status, price)
    if pf:
        facts.append(pf)
    asks = [h.get("ask") for h in history if h["speaker"] == "seller" and h.get("ask")]
    summary = {
        "facts": len(facts),
        "hedged": sum(f["hedged"] for f in facts),
        "contradictions": sum(f["contradicts_listing"] for f in facts),
        "changed": sum(f["changed_during_call"] for f in facts),
        "low": sum(f["level"] == "low" for f in facts),
        "reliability": round(sum(f["confidence"] for f in facts) / len(facts), 2) if facts else None,
        "seller_turns": len(seller_turns),
        "price_path": asks,
        "bargain": (listing["price"] - price) if status == "agreed" else None,
    }
    return facts, summary
