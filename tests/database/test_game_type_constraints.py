"""Database-enforced uniqueness and referential integrity for game types."""

import uuid
from datetime import datetime

import pytest
from sqlalchemy.exc import IntegrityError

from app.database import SessionLocal
from app.models import EntityType, GameType


def test_game_type_name_can_repeat_across_versions_but_not_same_version():
    with SessionLocal() as session:
        session.add_all(
            [
                GameType(name="alpha", version=1, is_generic=False),
                GameType(name="alpha", version=2, is_generic=False),
            ]
        )
        session.commit()

        assert session.query(GameType).filter_by(name="alpha", version=1).count() == 1
        assert session.query(GameType).filter_by(name="alpha", version=2).count() == 1

    with SessionLocal() as session:
        session.add(GameType(name="alpha", version=2, is_generic=False))
        with pytest.raises(Exception):
            session.commit()


def test_active_game_types_are_unique_by_name_and_version(db_session):
    db_session.add_all([GameType(name="core_game", version=1), GameType(name="core_game", version=1)])
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_deprecated_game_types_are_unique_by_name_version_and_deprecated_at(db_session):
    deprecated_at = datetime(2026, 1, 1, 12, 0, 0)
    db_session.add_all(
        [
            GameType(name="core_game", version=1),
            GameType(name="core_game", version=1, deprecated_at=deprecated_at),
            GameType(name="core_game", version=1, deprecated_at=datetime(2026, 1, 2)),
        ]
    )
    db_session.commit()

    db_session.add(GameType(name="core_game", version=1, deprecated_at=deprecated_at))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_database_rejects_rows_for_nonexistent_game_type(db_session):
    db_session.add(EntityType(name="orphan", game_type_id=uuid.uuid4()))
    with pytest.raises(IntegrityError):
        db_session.commit()
