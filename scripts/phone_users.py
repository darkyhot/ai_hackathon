"""Ручная модерация звонков на телефон.

python scripts/phone_users.py list
python scripts/phone_users.py grant user@example.ru
python scripts/phone_users.py revoke user@example.ru

Изменение действует сразу, перезапуск не нужен: доступ проверяется при каждом запросе.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select  # noqa: E402

from backend import db  # noqa: E402
from backend.db.models import User  # noqa: E402


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("list", "grant", "revoke") or (argv[0] != "list" and len(argv) != 2):
        print(__doc__.strip())
        return 2
    db.configure()
    with db.session() as s:
        if argv[0] == "list":
            for u in s.scalars(select(User).order_by(User.id)):
                print(f"{'+' if u.phone_allowed else '-'} {u.email}  ({u.name or ''}, с {u.created_at:%d.%m.%Y})")
            return 0
        user = s.scalar(select(User).where(func.lower(User.email) == argv[1].strip().casefold()))
        if user is None:
            print(f"Пользователь {argv[1]} не найден")
            return 1
        user.phone_allowed = argv[0] == "grant"
        print(f"{user.email}: звонки на телефон {'разрешены' if user.phone_allowed else 'запрещены'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
