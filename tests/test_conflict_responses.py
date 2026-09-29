"""Every 409 response lists all of the resources that blocked the request, so it can be revised and resubmitted."""

import pytest
from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models import EntityType, GameType

client = TestClient(app)

GENERIC = {"name": "test_generic_game_type", "version": 1}
TARGET = {"name": "test_game_type", "version": 1}
EQUIP_SETUP = pytest.mark.parametrize(
    "game_types, bulk_game_type_bundle",
    [pytest.param([{**GENERIC, "is_generic": True}, TARGET], ["tests/fixtures/bulk_game_types/equip_generic_bundle.json"], id="equip")],
    indirect=True,
)


def _url(game_type, path):
    return f"/api/v1/game-types/{game_type['name']}/versions/{game_type['version']}/config/{path}"


def _by_kind_and_name(blocking_resources):
    return {(item["kind"], item.get("name")): item for item in blocking_resources}


@pytest.mark.parametrize("game_types", [["core_game"]], indirect=True)
def test_create_duplicate_game_type_lists_the_existing_game_type(game_types):
    response = client.post("/api/v1/game-types", json={"name": "core_game", "version": 1})

    assert response.status_code == 409
    body = response.json()
    assert body["warning_type"] == "game_type_exists"
    [existing] = body["blocking_resources"]
    assert existing["kind"] == "game_type"
    assert existing["id"] == game_types[0]["id"]
    assert existing["name"] == "core_game"
    assert existing["fields"]["version"] == 1
    assert existing["reason"]


def test_delete_game_type_lists_every_config_resource_still_under_it():
    for payload in ({"name": "core_game", "version": 1}, {"name": "dm_house_rules", "version": 1, "derived_from": {"name": "core_game", "version": 1}}):
        assert client.post("/api/v1/game-types", json=payload).status_code == 201
    with SessionLocal() as session:
        core_game = session.query(GameType).filter_by(name="core_game").one()
        session.add_all([EntityType(name="sword", game_type_id=core_game.id), EntityType(name="orc", game_type_id=core_game.id)])
        session.commit()

    response = client.delete("/api/v1/game-types/core_game/versions/1")

    # Only config blocks a delete; the derived game type would make it a deprecation once the config is gone.
    assert response.status_code == 409
    body = response.json()
    assert body["warning_type"] == "game_type_has_config"
    blockers = _by_kind_and_name(body["blocking_resources"])
    assert set(blockers) == {("entity_type", "sword"), ("entity_type", "orc")}
    assert blockers[("entity_type", "sword")]["via"] == "entity_type.game_type_id"
    assert blockers[("entity_type", "sword")]["game_type"]["name"] == "core_game"
    assert body["details"]["references"] == {"entity_type.game_type_id": 2}


@pytest.mark.parametrize("game_types", [["weapons", {"name": "core_game", "derived_from": "weapons"}]], indirect=True)
def test_create_derived_from_derived_game_type_lists_the_ancestry(game_types):
    response = client.post("/api/v1/game-types", json={"name": "dm_house_rules", "version": 1, "derived_from": {"name": "core_game", "version": 1}})

    assert response.status_code == 409
    body = response.json()
    assert body["warning_type"] == "derivation_depth_exceeded"
    assert [(item["name"], item["reason"]) for item in body["blocking_resources"]] == [
        ("core_game", "the requested parent is itself a derived game type"),
        ("weapons", "ancestor of 'core_game' v1"),
    ]


@EQUIP_SETUP
def test_import_collision_lists_every_existing_resource_in_the_target_game_type(game_types, bulk_game_type_bundle, test_body_rows):
    incoming_config = {"name": "equip_root", "resolver_key": "different_resolver", "action_type": "equip", "relationship_type": "root"}
    response = client.post(
        _url(GENERIC, "import"),
        json={
            "entity_types": [{"name": "sword"}, {"name": "shield"}],
            "relationship_types": [{"name": "equipped"}],
            "action_resolver_configs": [incoming_config],
        },
    )

    assert response.status_code == 409
    body = response.json()
    assert body["warning_type"] == "key_collision"
    blockers = _by_kind_and_name(body["blocking_resources"])
    assert set(blockers) == {("entity_type", "sword"), ("relationship_type", "equipped"), ("action_resolver_config", "equip_root")}
    assert all(item["game_type"]["name"] == GENERIC["name"] for item in body["blocking_resources"])
    config = blockers[("action_resolver_config", "equip_root")]
    assert config["incoming"] == incoming_config
    assert config["current"]["resolver_key"] == "create_relationship"
    assert config["changed"] is True
    assert body["details"]["conflicts_by_kind"] == {"entity_type": ["sword"], "relationship_type": ["equipped"], "action_resolver_config": ["equip_root"]}


@EQUIP_SETUP
def test_import_does_not_treat_same_named_resources_in_other_game_types_as_collisions(game_types, bulk_game_type_bundle, test_body_rows):
    response = client.post(_url(TARGET, "import"), json={"entity_types": [{"name": "sword"}]})
    assert response.status_code == 200, response.text


@EQUIP_SETUP
def test_import_mismatch_lists_every_mismatched_part_of_the_payload(game_types, bulk_game_type_bundle, test_body_rows):
    response = client.post(
        _url(TARGET, "import/preview"),
        json={
            "game_type": {"name": "other_game", "version": 2},
            "entity_types": [{"name": "sword"}, {"name": "axe", "game_type": "third_game"}],
        },
    )

    assert response.status_code == 409
    body = response.json()
    assert body["warning_type"] == "game_type_mismatch"
    assert [(item["location"], item["value"]) for item in body["blocking_resources"]] == [
        ("game_type.name", "other_game"),
        ("game_type.version", 2),
        ("entity_types[1]", "third_game"),
    ]
    assert body["affected_game_types"] == ["other_game", "test_game_type", "third_game"]


@EQUIP_SETUP
@pytest.mark.parametrize("path", ["delete/preview", "delete"])
def test_delete_cascade_warning_lists_every_resource_and_its_dependents(game_types, bulk_game_type_bundle, test_body_rows, path):
    response = client.post(
        _url(GENERIC, path),
        json={"relationship_types": [{"name": "equipped"}], "action_types": [{"name": "equip"}], "action_resolver_configs": [{"name": "equip_root"}]},
    )

    assert response.status_code == 409
    body = response.json()
    assert body["warning_type"] == "cascade_warning"
    blockers = _by_kind_and_name(body["blocking_resources"])
    assert set(blockers) == {("relationship_type", "equipped"), ("action_type", "equip"), ("action_resolver_config", "equip_root")}

    def dependents(key):
        return sorted(item["via"] for item in blockers[key]["dependents"])

    assert dependents(("relationship_type", "equipped")) == [
        "relationship_effect_config.relationship_type_id",
        "relationship_type_participant_type.relationship_type_id",
        "relationship_type_participant_type.relationship_type_id",
        "relationship_type_participant_type.relationship_type_id",
    ]
    assert dependents(("action_type", "equip")) == ["action_resolver_config.action_type_id"]
    assert dependents(("action_resolver_config", "equip_root")) == ["action_effect_config.action_resolver_config_id"]
