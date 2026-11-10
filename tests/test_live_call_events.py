"""Живой звонок (телефон или «продавец — я»): события разговора уходят в браузер сразу.

Инцидент 10.11: звонок на телефон соединился, но агент молчал. Инструмент звонка
работает на копии диалога и копит события до конца шага; реплика агента и
await_seller ждали в буфере, а сервер в это время ждал ответа продавца.
"""
import unittest

from backend import db
from backend.agent import session
from backend.db.models import ChatEvent
from sqlalchemy import select


class LiveCallEventsTest(unittest.TestCase):
    def setUp(self):
        db.configure('sqlite://')
        self.owner = session.create_session('', None)
        self.owner.remember()

    def delivered(self):
        with db.session() as s:
            return [e.type for e in s.scalars(select(ChatEvent).where(ChatEvent.chat_id == self.owner.id).order_by(ChatEvent.seq))]

    def test_agent_line_and_wait_for_seller_reach_the_client_before_the_call_ends(self):
        tool = self.owner.fork()
        tool.push('call_start', seller='Тимур', mode='phone')
        tool.push('turn', speaker='agent', text='Здравствуйте! Квартира ещё продаётся?', offer=None, phase='clarify')
        tool.push('await_seller')
        self.assertEqual(self.delivered()[-3:], ['call_start', 'turn', 'await_seller'],
                         'Браузер должен получить реплику, чтобы произнести её в линию, и знак слушать продавца')
        tool.push('price', ask=19_000_000, offer=18_700_000)
        tool.push('guard', text='не выше лимита')
        self.assertEqual(self.delivered()[-2:], ['price', 'guard'])

    def test_ordinary_tool_messages_still_wait_for_the_step(self):
        tool = self.owner.fork()
        before = self.delivered()
        tool.push('message', role='agent', text='Промежуточный итог')
        self.assertEqual(self.delivered(), before)
        tool.flush()
        self.assertEqual(self.delivered()[-1], 'message')


if __name__ == '__main__':
    unittest.main()
