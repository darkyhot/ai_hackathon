"""Способ оплаты, угаданный моделью из свободного текста, — подсказка, а не решение клиента.

Инцидент 10.11: «1к квартира в Москве, бюджет до 25 млн, ЗАО» — модель записала «наличные»,
и перед звонком не было ни карточки оплаты, ни развилки по ипотеке.
"""
import copy
import unittest
from unittest.mock import AsyncMock

from backend import db
from backend.agent import capabilities, session, understanding
from backend.agent.profile import merge_profile, payment_confirmed
from backend.agent.workflow import next_search_action
from tests import test_products as TP
from tests.test_products import answer
from tests.test_llm_and_agent import listing


class PaymentConfirmationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        db.configure('sqlite://')

    def agent(self):
        s = session.create_session('', None)
        s.profile = merge_profile({'city': 'Казань', 'rooms': 1, 'budget_max': 9_000_000, 'housing': 'secondary'})
        s.profile['_approved_budget_max'] = s.profile['budget_max']
        s.red_lines_ok = True
        s.prefs.sync(s.profile)
        x = listing(city='Казань', price=8_500_000, is_new=False)
        s.search = {'listings': [x], 'pool': [x], 'shown_ids': {x['id']}, 'meta': {}, 'comps': [x]}
        s.shown = [x]
        return s, x

    async def review(self, s, changes, source, text='1к квартира в Казани, бюджет до 9 млн'):
        seq = s.request_review_seq + 1
        s.request_inputs = [{'seq': seq, 'text': text, 'question': ''}]
        s.request_review = {'seq': seq, 'base_profile': copy.deepcopy(s.profile), 'intent': 'refine', 'decision': 'apply',
                            'changes': changes, 'question': '', 'options': [], 'selection': None, 'call_ids': None,
                            'unsupported': [], 'source': source}
        return await understanding.ensure(s)

    def ask_call(self, s, x):
        s.selection_profile_stale = False  # подборку уже обновили под условия
        s.profile.pop('_selection_stale', None)
        s.request_intent = 'call'
        s.call_targets = [x['id']]

    async def test_model_guess_is_a_suggestion_and_the_card_appears_before_the_call(self):
        s, x = self.agent()
        await self.review(s, {'payment': 'cash'}, 'model')
        self.assertEqual(s.profile['payment'], 'cash', 'догадка сохраняется — по ней считаем подборку')
        self.assertFalse(payment_confirmed(s.profile))
        self.ask_call(s, x)
        allowed, reason = capabilities.decision(s, 'call_seller')
        self.assertFalse(allowed)
        self.assertIn('подтвердите', reason)
        self.assertEqual(next_search_action(s), 'ask_payment')
        ask = answer(True, payment='mortgage_saving')
        await TP.ProductsTest.act(self, s, 'ask_payment', ask=ask)
        kind, title, text = ask.call_args.args
        self.assertEqual(ask.call_args.kwargs['suggested'], 'cash')
        self.assertIn('наличными', text)
        self.assertEqual(s.profile['payment'], 'mortgage_saving')
        self.assertTrue(payment_confirmed(s.profile))
        self.assertFalse(capabilities.decision(s, 'ask_payment')[0], 'второй раз карточку не показываем')

    async def test_explicit_rule_parse_or_card_chip_needs_no_extra_card(self):
        for source in ('rules', 'bound_card_choice'):
            s, x = self.agent()
            await self.review(s, {'payment': 'cash'}, source, text='однушка в Казани до 9 млн, наличными')
            self.assertTrue(payment_confirmed(s.profile), source)
            self.ask_call(s, x)
            self.assertNotEqual(next_search_action(s), 'ask_payment', source)

    async def test_typed_answer_to_the_payment_card_is_the_clients_decision(self):
        s, x = self.agent()
        self.ask_call(s, x)
        result = await TP.ProductsTest.act(self, s, 'ask_payment', ask=answer(None, text='за свои'))
        self.assertEqual(result['client_said'], 'за свои')
        await self.review(s, {'payment': 'cash'}, 'model', text='за свои')
        self.assertTrue(payment_confirmed(s.profile))
        # Флаг ответа на карточку одноразовый: следующая догадка снова требует подтверждения.
        await self.review(s, {'payment': 'mortgage_ready'}, 'model', text='бюджет до 9 млн')
        self.assertFalse(payment_confirmed(s.profile))

    def test_profiles_without_the_flag_stay_confirmed(self):
        self.assertTrue(payment_confirmed({'payment': 'cash'}))
        self.assertFalse(payment_confirmed({'payment': None}))


if __name__ == '__main__':
    unittest.main()
