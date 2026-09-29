import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.config_import_export import export_game_type_config, import_game_type_config
from app.database import SessionLocal
from app.main import app
from app.models import EntityType, GameType
from app.resolution import run_action_resolution

client = TestClient(app)


@pytest.mark.parametrize(
    "game_types",
    [
        [{"name": "core_game"}],
        [{"name": "weapons", "is_generic": True}],
        [
            {"name": "weapons", "is_generic": True},
            {"name": "core_game"},
            {"name": "dm_house_rules", "derived_from": {"name": "core_game", "version": 1}},
            {"name": "table_two_rules", "derived_from": "core_game"},
        ],
        [
            {"name": "core_game", "version": 1},
            {"name": "core_game", "version": 2},
            {"name": "dm_house_rules", "derived_from": {"name": "core_game", "version": 2}},
        ],
    ],
    indirect=True,
)
def test_game_types_fixture_creates_requested_game_types(request, game_types):
    specs = request.node.callspec.params["game_types"]
    assert len(game_types) == len(specs)

    with SessionLocal() as session:
        for spec, created in zip(specs, game_types):
            row = session.query(GameType).filter_by(name=spec["name"], version=spec.get("version", 1)).one()
            assert str(row.id) == created["id"]
            assert row.is_generic == spec.get("is_generic", False)

            derived_from = spec.get("derived_from")
            if derived_from is None:
                assert row.derived_from_id is None
                assert created["derived_from"] is None
            else:
                if isinstance(derived_from, str):
                    derived_from = {"name": derived_from}
                assert row.derived_from.name == derived_from["name"]
                assert row.derived_from.version == derived_from.get("version", 1)
                assert created["derived_from"]["id"] == str(row.derived_from_id)


def test_game_types_fixture_defaults_to_single_non_generic_game_type(game_types):
    assert len(game_types) == 1
    assert game_types[0]["is_generic"] is False
    assert game_types[0]["derived_from"] is None


@pytest.mark.parametrize("game_types", [[{"name": "core_game"}]], indirect=True)
def test_create_game_type_rejects_duplicate_name_and_version(game_types):
    response = client.post("/api/v1/game-types", json={"name": "core_game", "version": 1})
    assert response.status_code == 409


def test_create_game_type_rejects_missing_parent():
    response = client.post("/api/v1/game-types", json={"name": "dm_house_rules", "derived_from": {"name": "missing", "version": 1}})
    assert response.status_code == 404

    with SessionLocal() as session:
        assert session.query(GameType).count() == 0


def test_delete_game_type_returns_404_for_unknown_game_type():
    response = client.delete("/api/v1/game-types/missing/versions/1")
    assert response.status_code == 404


@pytest.mark.parametrize("game_types", [[{"name": "core_game"}]], indirect=True)
def test_create_game_type_requires_parent_version(game_types):
    response = client.post("/api/v1/game-types", json={"name": "dm_house_rules", "derived_from": {"name": "core_game"}})
    assert response.status_code == 422


@pytest.mark.parametrize("game_types", [[{"name": "core_game"}]], indirect=True)
def test_game_type_endpoints_only_match_the_requested_version(game_types):
    assert client.post("/api/v1/game-types/core_game/versions/2/config/export").status_code == 404
    assert client.delete("/api/v1/game-types/core_game/versions/2").status_code == 404


@pytest.mark.parametrize("game_types", [[{"name": "weapons"}, {"name": "core_game"}]], indirect=True)
def test_copy_requires_source_version(game_types):
    response = client.post(
        "/api/v1/game-types/core_game/versions/1/config/copy",
        json={"source_game_type": {"name": "weapons"}, "entity_types": [{"name": "sword"}]},
    )
    assert response.status_code == 422


def test_bulk_import_targets_only_the_requested_version():
    for version in (1, 2):
        response = client.post("/api/v1/game-types", json={"name": "core_game", "version": version})
        assert response.status_code == 201
    v2_id = response.json()["id"]

    response = client.post("/api/v1/game-types/core_game/versions/2/config/import", json={"entity_types": [{"name": "sword"}]})
    assert response.status_code == 200
    assert response.json()["game_type_id"] == v2_id

    with SessionLocal() as session:
        assert [str(row.game_type_id) for row in session.query(EntityType).all()] == [v2_id]


def test_bulk_import_rejects_payload_for_a_different_version():
    response = client.post("/api/v1/game-types", json={"name": "core_game", "version": 1})
    assert response.status_code == 201

    response = client.post(
        "/api/v1/game-types/core_game/versions/1/config/import",
        json={"game_type": {"name": "core_game", "version": 2}, "entity_types": [{"name": "sword"}]},
    )
    assert response.status_code == 409
    assert response.json()["warning_type"] == "game_type_mismatch"

    with SessionLocal() as session:
        assert session.query(EntityType).count() == 0


def test_delete_game_type_is_blocked_while_resources_reference_it():
    response = client.post("/api/v1/game-types", json={"name": "core_game"})
    assert response.status_code == 201

    with SessionLocal() as session:
        game_type = session.query(GameType).filter_by(name="core_game").one()
        session.add(EntityType(name="sword", game_type_id=game_type.id))
        session.commit()

    response = client.delete("/api/v1/game-types/core_game/versions/1")
    assert response.status_code == 409
    assert response.json()["warning_type"] == "game_type_has_config"
    assert response.json()["details"]["references"] == {"entity_type.game_type_id": 1}

    with SessionLocal() as session:
        assert session.query(GameType).filter_by(name="core_game").count() == 1


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
def test_bulk_endpoints_require_existing_game_type(path, payload):
    response = client.post(f"/api/v1/game-types/missing/versions/1/{path}", json=payload)
    assert response.status_code == 404

    with SessionLocal() as session:
        assert session.query(GameType).count() == 0


def test_bulk_import_populates_existing_game_type_without_creating_one():
    response = client.post("/api/v1/game-types", json={"name": "core_game"})
    assert response.status_code == 201
    game_type_id = response.json()["id"]

    response = client.post(
        "/api/v1/game-types/core_game/versions/1/config/import",
        json={"game_type": {"name": "core_game", "version": 1}, "entity_types": [{"name": "sword"}]},
    )
    assert response.status_code == 200
    assert response.json()["game_type_id"] == game_type_id

    with SessionLocal() as session:
        assert session.query(GameType).count() == 1
        assert session.query(EntityType).filter_by(name="sword").one().game_type_id == uuid.UUID(game_type_id)


def test_bulk_delete_leaves_game_type_in_place():
    response = client.post("/api/v1/game-types", json={"name": "core_game"})
    assert response.status_code == 201

    response = client.post("/api/v1/game-types/core_game/versions/1/config/delete", json={"entity_types": [{"name": "sword"}]})
    assert response.status_code == 200

    with SessionLocal() as session:
        assert session.query(GameType).filter_by(name="core_game").count() == 1


def test_library_import_and_export_require_existing_game_type():
    with SessionLocal() as session:
        with pytest.raises(ValueError, match="does not exist"):
            import_game_type_config(session, "missing", 1, {"entity_types": [{"name": "sword"}]})
        with pytest.raises(ValueError, match="does not exist"):
            export_game_type_config(session, "missing", 1)
        assert session.query(GameType).count() == 0
        assert session.query(EntityType).count() == 0


def test_database_rejects_rows_for_nonexistent_game_type():
    with SessionLocal() as session:
        session.add(EntityType(name="orphan", game_type_id=uuid.uuid4()))
        with pytest.raises(IntegrityError):
            session.commit()


def test_run_action_resolution_requires_existing_game_type():
    with SessionLocal() as session:
        with pytest.raises(ValueError, match="game_type_id does not exist"):
            run_action_resolution(
                session,
                game_type_id=uuid.uuid4(),
                action_type_id=uuid.uuid4(),
                participants={},
                action_resolver_config_id=uuid.uuid4(),
            )
