import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, tuple_

from app.config_import_export import import_game_type_config
from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import GameType

from helpers import GameTypeSpec


@pytest.fixture(autouse=True)
def reset_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


def _table_primary_keys():
    with engine.connect() as conn:
        return {
            table.name: set(conn.execute(select(*table.primary_key.columns)).all())
            for table in Base.metadata.sorted_tables
        }


def _rows_added_since(rows_before):
    return {name: rows - rows_before[name] for name, rows in _table_primary_keys().items()}


def _delete_rows(rows_by_table):
    """Delete the given rows, one statement per table, children before parents."""
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            rows = rows_by_table.get(table.name)
            if rows:
                conn.execute(delete(table).where(tuple_(*table.primary_key.columns).in_([tuple(row) for row in rows])))


@pytest.fixture
def db_session():
    """A session for the test and the fixtures that set up its data.

    The API commits through its own sessions, so objects this session already loaded can go
    stale after an API call that changes them; call ``expire_all()`` before reading them again.
    Fixtures roll this session back before their teardown touches the database.
    """
    with SessionLocal() as session:
        yield session
        session.rollback()


@pytest.fixture
def game_type_specs(request):
    """The game type specs a test works with, without creating anything.

    Parametrize indirectly with a list of ``GameTypeSpec`` objects; a ``derived_from`` must
    also be a ``GameTypeSpec``. Specs are taken exactly as given, with no defaults filled in,
    so they can describe data the API should reject. Tests where game type creation is in
    scope use these directly; ``game_types`` creates them for tests where it is setup.

    Defaults to a single ``test_game`` version 1 when not parametrized.
    """
    specs = getattr(request, "param", None) or [GameTypeSpec(name="test_game", version=1)]
    for spec in specs:
        if not isinstance(spec, GameTypeSpec):
            raise TypeError(f"game_type_specs must be GameTypeSpec instances, got {spec!r}")
        if spec.parent is not None and not isinstance(spec.parent, GameTypeSpec):
            raise TypeError(f"derived_from must be a GameTypeSpec, got {spec.parent!r}")
    return specs


@pytest.fixture
def game_types(game_type_specs, db_session):
    """Create the ``game_type_specs`` game types through the API and delete them through the API on teardown.

    Each spec's ``create_request`` is sent as is, in order, so a parent must appear earlier
    in the list than any game type derived from it. Every creation must succeed; tests of
    rejected creations use ``game_type_specs`` directly.

    Yields the created game types as ``GameType`` objects loaded in ``db_session``, in spec order.
    Teardown deletes them in reverse order and asserts every table holds exactly the
    rows it held before setup.
    """
    client = TestClient(app)
    rows_before = _table_primary_keys()

    created = []
    try:
        for spec in game_type_specs:
            response = client.post("/api/v1/game-types", json=spec.create_request)
            assert response.status_code == 201, f"Failed to create game type {spec}: {response.text}"
            created.append(response.json())

        yield [db_session.get(GameType, uuid.UUID(game_type["id"])) for game_type in created]
    finally:
        db_session.rollback()
        failures = []
        for game_type in reversed(created):
            # A test may have deleted the game type itself, or deprecated it, which changes the address it is deleted through.
            matches = client.get(f"/api/v1/game-types/by-id/{game_type['id']}").json()
            if not matches:
                continue
            [current] = matches
            path = f"/api/v1/game-types/{current['name']}/versions/{current['version']}"
            if current["deprecated_at"] is not None:
                path += f"/deprecated/{current['deprecated_at']}"
            response = client.delete(path)
            if response.status_code != 200 or response.json().get("status") != "deleted":
                failures.append(f"{game_type['name']} v{game_type['version']}: {response.status_code} {response.text}")
        assert not failures, "Failed to delete game types:\n" + "\n".join(failures)
        assert _table_primary_keys() == rows_before, "game_types fixture teardown did not restore the database to its pre-setup state"


@pytest.fixture
def bulk_game_type_bundle(request, game_types, db_session):
    """Import config bundles into game types that the ``game_types`` fixture created.

    Parametrize indirectly with one or more bundle paths, together with ``game_type_specs``
    for every game type the bundles target. Each bundle's ``game_type`` block names the
    existing game type (``name`` and ``version``) it is imported into. This fixture never
    creates or deletes game types.

    Yields the loaded bundles. Teardown deletes exactly the rows the imports added and
    asserts every table holds exactly the rows it held before the imports.
    """
    bundle_paths = getattr(request, "param", None)
    if not bundle_paths:
        raise ValueError("bulk_game_type_bundle must be parametrized with one or more bundle paths")
    if isinstance(bundle_paths, (str, Path)):
        bundle_paths = [bundle_paths]

    bundles = []
    for bundle_path in bundle_paths:
        with Path(bundle_path).open("r", encoding="utf-8") as handle:
            bundles.append(json.load(handle))

    rows_before = _table_primary_keys()
    rows_added = {}
    try:
        for payload in bundles:
            target = payload["game_type"]
            import_game_type_config(db_session, target["name"], target["version"], payload)
        db_session.commit()
        rows_added = _rows_added_since(rows_before)

        yield bundles
    finally:
        db_session.rollback()
        _delete_rows(rows_added)
        assert _table_primary_keys() == rows_before, "bulk_game_type_bundle teardown did not restore the database to its pre-import state"


@pytest.fixture
def test_body_rows(request, db_session):
    """Delete every row the test body creates, and only those rows.

    Snapshots the database once the test's other setup fixtures (``game_types`` and
    ``bulk_game_type_bundle``, when the test uses them) have run, so it tears down before
    them. Teardown deletes the rows added since the snapshot and asserts every table holds
    exactly the rows it held at the snapshot, which also catches setup rows the body deleted.
    """
    for fixture_name in ("game_types", "bulk_game_type_bundle"):
        if fixture_name in request.fixturenames:
            request.getfixturevalue(fixture_name)

    rows_before = _table_primary_keys()
    try:
        yield
    finally:
        db_session.rollback()
        _delete_rows(_rows_added_since(rows_before))
        assert _table_primary_keys() == rows_before, "test_body_rows teardown did not restore the database to its pre-test state"
