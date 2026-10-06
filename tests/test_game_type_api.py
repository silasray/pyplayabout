import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.config_import_export import export_game_type_config, import_game_type_config
from app.main import app
from app.models import EntityType, GameType
from app.resolution import run_action_resolution

from helpers import GameTypeSpec

client = TestClient(app)


@pytest.mark.parametrize(
    "game_type_specs",
    [
        [GameTypeSpec(name="core_game", version=1)],
        [GameTypeSpec(name="weapons", version=1, is_generic=True)],
        [
            GameTypeSpec(name="weapons", version=1, is_generic=True),
            GameTypeSpec(name="core_game", version=1),
            GameTypeSpec(name="dm_house_rules", version=1, derived_from=GameTypeSpec(name="core_game", version=1)),
            GameTypeSpec(name="table_two_rules", version=1, derived_from=GameTypeSpec(name="core_game", version=1)),
        ],
        [
            GameTypeSpec(name="core_game", version=1),
            GameTypeSpec(name="core_game", version=2),
            GameTypeSpec(name="dm_house_rules", version=1, derived_from=GameTypeSpec(name="core_game", version=2)),
        ],
    ],
    indirect=True,
)
def test_game_types_fixture_creates_requested_game_types(game_type_specs, game_types):
    assert len(game_types) == len(game_type_specs)

    for spec, game_type in zip(game_type_specs, game_types):
        assert game_type.signature == spec.signature
        assert game_type.is_generic == spec.get("is_generic", False)

        if spec.parent is None:
            assert game_type.derived_from is None
        else:
            assert game_type.derived_from.signature == spec.parent.signature
            assert game_type.derived_from in game_types


def test_game_types_fixture_defaults_to_single_non_generic_game_type(game_types):
    assert len(game_types) == 1
    assert game_types[0].is_generic is False
    assert game_types[0].derived_from is None


@pytest.mark.parametrize("game_type_specs", [[GameTypeSpec(name="core_game", version=1)]], indirect=True)
def test_create_game_type_rejects_duplicate_name_and_version(game_type_specs, game_types):
    response = client.post("/api/v1/game-types", json=game_type_specs[0].create_request)
    assert response.status_code == 409


@pytest.mark.parametrize("game_type_specs", [[GameTypeSpec(name="dm_house_rules", derived_from=GameTypeSpec(name="missing", version=1))]], indirect=True)
def test_create_game_type_rejects_missing_parent(game_type_specs, db_session):
    response = client.post("/api/v1/game-types", json=game_type_specs[0].create_request)
    assert response.status_code == 404

    assert db_session.query(GameType).count() == 0


def test_delete_game_type_returns_404_for_unknown_game_type():
    response = client.delete("/api/v1/game-types/missing/versions/1")
    assert response.status_code == 404


@pytest.mark.parametrize("game_type_specs", [[GameTypeSpec(name="core_game", version=1)]], indirect=True)
def test_create_game_type_requires_parent_version(game_type_specs, game_types):
    [core_game] = game_type_specs
    unversioned_parent = GameTypeSpec(name=core_game["name"])
    response = client.post("/api/v1/game-types", json=GameTypeSpec(name="dm_house_rules", derived_from=unversioned_parent).create_request)
    assert response.status_code == 422


@pytest.mark.parametrize("game_type_specs", [[GameTypeSpec(name="core_game", version=1)]], indirect=True)
def test_game_type_endpoints_only_match_the_requested_version(game_types):
    assert client.post("/api/v1/game-types/core_game/versions/2/config/export").status_code == 404
    assert client.delete("/api/v1/game-types/core_game/versions/2").status_code == 404


@pytest.mark.parametrize("game_type_specs", [[GameTypeSpec(name="weapons", version=1), GameTypeSpec(name="core_game", version=1)]], indirect=True)
def test_copy_requires_source_version(game_types):
    response = client.post(
        "/api/v1/game-types/core_game/versions/1/config/copy",
        json={"source_game_type": {"name": "weapons"}, "entity_types": [{"name": "sword"}]},
    )
    assert response.status_code == 422


@pytest.mark.parametrize("game_type_specs", [[GameTypeSpec(name="core_game", version=1), GameTypeSpec(name="core_game", version=2)]], indirect=True)
def test_bulk_import_targets_only_the_requested_version(game_types, test_body_rows, db_session):
    v2_id = str(game_types[1].id)

    response = client.post("/api/v1/game-types/core_game/versions/2/config/import", json={"entity_types": [{"name": "sword"}]})
    assert response.status_code == 200
    assert response.json()["game_type_id"] == v2_id

    assert [str(row.game_type_id) for row in db_session.query(EntityType).all()] == [v2_id]


@pytest.mark.parametrize("game_type_specs", [[GameTypeSpec(name="core_game", version=1)]], indirect=True)
def test_bulk_import_rejects_payload_for_a_different_version(game_types, db_session):
    response = client.post(
        "/api/v1/game-types/core_game/versions/1/config/import",
        json={"game_type": {"name": "core_game", "version": 2}, "entity_types": [{"name": "sword"}]},
    )
    assert response.status_code == 409
    assert response.json()["warning_type"] == "game_type_mismatch"

    assert db_session.query(EntityType).count() == 0


@pytest.mark.parametrize("game_type_specs", [[GameTypeSpec(name="core_game", version=1)]], indirect=True)
def test_delete_game_type_is_blocked_while_resources_reference_it(game_types, test_body_rows, db_session):
    [game_type] = game_types
    db_session.add(EntityType(name="sword", game_type_id=game_type.id))
    db_session.commit()

    response = client.delete("/api/v1/game-types/core_game/versions/1")
    assert response.status_code == 409
    assert response.json()["warning_type"] == "game_type_has_config"
    assert response.json()["details"]["references"] == {"entity_type.game_type_id": 1}

    assert db_session.query(GameType).filter_by(name="core_game").count() == 1


@pytest.mark.parametrize(
    "path, payload",
    [
        ("config/import/preview", {"entity_types": [{"name": "sword"}]}),
        ("config/import", {"entity_types": [{"name": "sword"}]}),
        ("config/delete/preview", {"entity_types": [{"name": "sword"}]}),
        ("config/delete", {"entity_types": [{"name": "sword"}]}),
        ("config/copy", {"source_game_type": {"name": "missing_source", "version": 1}, "entity_types": [{"name": "sword"}]}),
        ("config/export", None),
    ],
)
def test_bulk_endpoints_require_existing_game_type(path, payload, db_session):
    response = client.post(f"/api/v1/game-types/missing/versions/1/{path}", json=payload)
    assert response.status_code == 404

    assert db_session.query(GameType).count() == 0


@pytest.mark.parametrize("game_type_specs", [[GameTypeSpec(name="core_game", version=1)]], indirect=True)
def test_bulk_import_populates_existing_game_type_without_creating_one(game_types, test_body_rows, db_session):
    game_type_id = str(game_types[0].id)

    response = client.post(
        "/api/v1/game-types/core_game/versions/1/config/import",
        json={"game_type": {"name": "core_game", "version": 1}, "entity_types": [{"name": "sword"}]},
    )
    assert response.status_code == 200
    assert response.json()["game_type_id"] == game_type_id

    assert db_session.query(GameType).count() == 1
    assert db_session.query(EntityType).filter_by(name="sword").one().game_type_id == uuid.UUID(game_type_id)


@pytest.mark.parametrize("game_type_specs", [[GameTypeSpec(name="core_game", version=1)]], indirect=True)
def test_bulk_delete_leaves_game_type_in_place(game_types, db_session):
    response = client.post("/api/v1/game-types/core_game/versions/1/config/delete", json={"entity_types": [{"name": "sword"}]})
    assert response.status_code == 200

    assert db_session.query(GameType).filter_by(name="core_game").count() == 1


def test_library_import_and_export_require_existing_game_type(db_session):
    with pytest.raises(ValueError, match="does not exist"):
        import_game_type_config(db_session, "missing", 1, {"entity_types": [{"name": "sword"}]})
    with pytest.raises(ValueError, match="does not exist"):
        export_game_type_config(db_session, "missing", 1)
    assert db_session.query(GameType).count() == 0
    assert db_session.query(EntityType).count() == 0


def test_database_rejects_rows_for_nonexistent_game_type(db_session):
    db_session.add(EntityType(name="orphan", game_type_id=uuid.uuid4()))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_run_action_resolution_requires_existing_game_type(db_session):
    with pytest.raises(ValueError, match="game_type_id does not exist"):
        run_action_resolution(
            db_session,
            game_type_id=uuid.uuid4(),
            action_type_id=uuid.uuid4(),
            participants={},
            action_resolver_config_id=uuid.uuid4(),
        )
