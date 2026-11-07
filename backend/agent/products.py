"""Финансовые продукты под выбранную квартиру и сравнение обслуживания в Сбере с рынком.

Два момента в диалоге:
1. Клиент выбрал квартиру из подборки — предлагаем продукты, которые закрывают его
   денежную ситуацию: страхование жизни и имущества, вклад с выгодной ставкой и
   накопительный счёт (если на первый взнос не хватает), бонусы Спасибо,
   СберПрайм и предложения партнёров.
2. Клиент определился окончательно — показываем, во сколько обойдётся обслуживание
   сделки в Сбере и в среднем банке-конкуренте: комиссия за оформление, страховки,
   скидки к ставке, платёж и переплата за кредит.

Все числа — демо-допущения из calculation_policy.json (раздел products). Логика
подобрана так, чтобы предложение Сбера было лучше среднего по рынку, но не в убыток
банку: скидка к ставке меньше разницы, которую рынок закладывает в свою ставку, а
бонусы — маркетинговые, а не возврат комиссии.
"""
from __future__ import annotations

from ..calculation_policy import POLICY, metadata
from .mortgage import annuity

SBER = POLICY["products"]["sber"]
MARKET = POLICY["products"]["market"]
CROSS = POLICY["products"]["cross"]


def _round(value: float, step: int = 100) -> int:
    return int(round(value / step) * step)


def _months_to(target: float, start: float, monthly: float, rate: float, cap: int = 120) -> int | None:
    """Через сколько месяцев накопления с пополнениями закроют нехватку."""
    balance = start
    for month in range(cap + 1):
        if balance >= target:
            return month
        balance = balance * (1 + rate / 12) + monthly
    return None


def _interest(start: float, monthly: float, rate: float, months: int) -> int:
    """Процентный доход за срок: остаток растёт с пополнениями, тело вклада не считается доходом."""
    balance = start
    contributed = start
    for _ in range(months):
        balance = balance * (1 + rate / 12) + monthly
        contributed += monthly
    return int(round(balance - contributed))


def insurance(price: int, loan: int) -> dict:
    """Страхование на первый год: жизнь — от суммы кредита, имущество — от цены квартиры."""
    life_sber = _round(loan * SBER["life_rate"]) if loan else 0
    life_market = _round(loan * MARKET["life_rate"]) if loan else 0
    property_sber = _round(price * SBER["property_rate"])
    property_market = _round(price * MARKET["property_rate"])
    return {
        "life": {"sber": life_sber, "market": life_market},
        "property": {"sber": property_sber, "market": property_market},
        "sber_year": life_sber + property_sber,
        "market_year": life_market + property_market,
        "saving": life_market + property_market - life_sber - property_sber,
        "note": "Жизнь — от остатка кредита, имущество — от цены квартиры; расчёт за первый год.",
    }


def paperwork(price: int, loan: int) -> dict:
    """Расходы на оформление сделки: Сбер (СБР, регистрация, оценка) против среднего банка."""
    sber = {
        "Сервис безопасных расчётов": SBER["sbr"],
        "Электронная регистрация": SBER["registration"],
    }
    market = {
        "Комиссия за оформление ипотеки": max(MARKET["deal_fee_min"], _round(loan * MARKET["deal_fee_pct"])),
        "Юридическое сопровождение": MARKET["legal"],
    }
    if loan:
        sber["Оценка залога"] = SBER["appraisal"]
        market["Оценка залога"] = MARKET["appraisal"]
    sber_total, market_total = sum(sber.values()), sum(market.values())
    return {
        "sber": sber,
        "market": market,
        "sber_total": sber_total,
        "market_total": market_total,
        "saving": market_total - sber_total,
    }


def savings(down: int, gap: int, monthly: int, months: int | None = None) -> dict:
    """Деньги до сделки или нехватка взноса: вклад и накопительный счёт Сбера против рынка.

    gap > 0 — на взнос не хватает: показываем, за сколько месяцев закроем разрыв вкладом
    с пополнениями. gap == 0 — деньги лежат до сделки, пока идём к регистрации.
    """
    promo = SBER["promo_deposit"]
    market_rate = MARKET["deposit_rate"]
    if gap > 0:
        months_to_goal = _months_to(gap, 0, monthly, promo["rate"]) if monthly else None
        horizon = months_to_goal or promo["term_months"]
        income = _interest(0, monthly, promo["rate"], horizon)
        market_income = _interest(0, monthly, market_rate, horizon)
        mode, title = "gap", f"Не хватает {gap} ₽ на взнос"
    else:
        horizon = SBER["parking_months"]
        income = _interest(down, 0, promo["rate"], horizon)
        market_income = _interest(down, 0, market_rate, horizon)
        months_to_goal = 0
        mode, title = "parking", "Деньги на взнос до сделки"
    return {
        "mode": mode,
        "title": title,
        "gap": gap,
        "base": down if mode == "parking" else 0,
        "monthly": monthly if mode == "gap" else 0,
        "months": months_to_goal,
        "horizon_months": horizon,
        "product": {"name": promo["name"], "rate": promo["rate"], "term_months": promo["term_months"]},
        "account": {"name": "Накопительный счёт", "rate": SBER["savings_account_rate"]},
        "market_rate": market_rate,
        "income": income,
        "market_income": market_income,
        "extra": income - market_income,
        "note": "Ставки — демо-допущения; доход не гарантирован.",
    }


def cross_sell() -> dict:
    """Бонусы и подписки, которые не связаны с оформлением сделки."""
    items = [
        {"name": "Бонусы Спасибо за покупку квартиры",
         "value": CROSS["spasibo_welcome"], "note": "зачисляются на счёт после регистрации сделки"},
        {"name": "Повышенный кэшбэк Спасибо на 3 месяца",
         "value": CROSS["spasibo_cashback"], "note": "категория «Дом и ремонт»"},
        {"name": f"СберПрайм бесплатно {CROSS['prime_months']} мес.",
         "value": CROSS["prime_months"] * CROSS["prime_month_price"], "note": "доставка, Okko, Звук и повышенный кэшбэк"},
        *[{"name": p["name"], "value": p["value"], "note": p["note"]} for p in CROSS["partners"]],
    ]
    return {"items": items, "total": sum(item["value"] for item in items)}


def compare(price: int, loan: int, rate: float | None, years: int) -> dict:
    """Обслуживание в Сбере против среднего банка-конкурента: ставка, платёж, комиссии, страховки."""
    ins = insurance(price, loan)
    doc = paperwork(price, loan)
    if not loan or not rate:
        return {
            "cash": True, "rate": None, "years": 0, "insurance": ins, "paperwork": doc,
            "payment": None, "interest": None, "discounts": [],
            "total_saving": doc["saving"] + ins["saving"],
            "note": "Без ипотеки сравниваем только оформление и страхование.",
        }
    market_rate = round(rate + MARKET["rate_spread"], 4)
    pay_sber, pay_market = annuity(loan, rate, years), annuity(loan, market_rate, years)
    interest_sber = pay_sber * 12 * years - loan
    interest_market = pay_market * 12 * years - loan
    monthly_saving = pay_market - pay_sber
    return {
        "cash": False,
        "rate": {"sber": round(rate, 4), "market": market_rate,
                 "discounts": [{"name": name, "value": value} for name, value in SBER["discounts"]]},
        "years": years,
        "payment": {"sber": _round(pay_sber), "market": _round(pay_market),
                    "saving_month": _round(monthly_saving), "saving_year": _round(monthly_saving * 12),
                    "saving_term": _round(monthly_saving * 12 * years)},
        "interest": {"sber": _round(interest_sber), "market": _round(interest_market),
                     "saving": _round(interest_market - interest_sber)},
        "insurance": ins,
        "paperwork": doc,
        "total_saving": doc["saving"] + ins["saving"] + _round(interest_market - interest_sber),
        "note": f"Сравнение с банком-конкурентом: ставка на {round(MARKET['rate_spread'] * 100, 1)} п.п. выше, "
                "комиссия за оформление и юрист — по средним условиям рынка.",
    }


def offer(price: int, down: int, loan: int, rate: float | None, years: int,
          gap: int = 0, monthly: int = 0) -> dict:
    """Пакет предложений к выбранной квартире: страховка, деньги и бонусы."""
    ins = insurance(price, loan)
    money = savings(down=down, gap=gap, monthly=monthly)
    cross = cross_sell()
    return {
        "assumptions": metadata(),
        "price": price,
        "down": down,
        "loan": loan,
        "insurance": ins,
        "savings": money,
        "cross": cross,
        "total_benefit": ins["saving"] + money["extra"] + cross["total"],
        "note": POLICY["products"]["note"],
    }
