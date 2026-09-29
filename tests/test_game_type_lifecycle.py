"""Game type deprecation, addressing of deprecated game types, and snapshot-based history records."""

import json
import uuid
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.database import SessionLocal
from app.main import app
from app.models import (
    ActionResolverConfig,
    ActionType,
    BaseActionResolver,
    EffectCalculationConfig,
    Entity,
    EntityType,
    GameType,
    GameTypeResourceChange,
    Resolution,
)
from app.resolution import run_action_resolution

client = TestClient(app)


class CountTargetsResolver(BaseActionResolver):
    __resolver_key__ = "lifecycle_count_targets"

    def __call__(self):
        return {"targets": len(self.participants.get("target", []))}


def _path(name, version=1, deprecated_at=None):
    path = f"/api/v1/game-types/{name}/versions/{version}"
    return f"{path}/deprecated/{deprecated_at}" if deprecated_at else path


def _deprecate(name, version=1):
    response = client.post(f"{_path(name, version)}/deprecate")
    assert response.status_code == 200, response.text
    return response.json()


def _add_history_record(game_type_id):
    with SessionLocal() as session:
        session.add(
            Resolution(
                game_type_id=uuid.UUID(game_type_id),
                action_resolver_config_id=uuid.uuid4(),
                action_resolver_config_name="retired_config",
                config_snapshot={"format_version": 1, "root": "retired_config", "action_resolver_configs": [], "relationship_types": []},
                snapshot_format_version=1,
                status="completed",
                created_at=datetime(2026, 1, 1),
            )
        )
        session.commit()


# Uniqueness


def test_active_game_types_are_unique_by_name_and_version():
    with SessionLocal() as session:
        session.add_all([GameType(name="core_game", version=1), GameType(name="core_game", version=1)])
        with pytest.raises(IntegrityError):
            session.commit()


def test_deprecated_game_types_are_unique_by_name_version_and_deprecated_at():
    deprecated_at = datetime(2026, 1, 1, 12, 0, 0)
    with SessionLocal() as session:
        session.add_all(
            [
                GameType(name="core_game", version=1),
                GameType(name="core_game", version=1, deprecated_at=deprecated_at),
                GameType(name="core_game", version=1, deprecated_at=datetime(2026, 1, 2)),
            ]
        )
        session.commit()

        session.add(GameType(name="core_game", version=1, deprecated_at=deprecated_at))
        with pytest.raises(IntegrityError):
            session.commit()


# Listing and addressing


@pytest.mark.parametrize("game_types", [["core_game", "retired_game"]], indirect=True)
def test_list_hides_deprecated_game_types_unless_requested(game_types):
    _deprecate("retired_game")

    assert [game_type["name"] for game_type in client.get("/api/v1/game-types").json()] == ["core_game"]
    listed = {game_type["name"]: game_type for game_type in client.get("/api/v1/game-types", params={"include_deprecated": True}).json()}
    assert set(listed) == {"core_game", "retired_game"}
    assert listed["core_game"]["deprecated_at"] is None
    assert listed["retired_game"]["deprecated_at"] is not None


@pytest.mark.parametrize(
    "game_types",
    [[{"name": "core_game", "version": 1}, {"name": "core_game", "version": 2}, "other_game"]],
    indirect=True,
)
def test_list_filters_by_name_and_by_name_and_version(game_types, test_body_rows):
    first_deprecation = _deprecate("core_game", 1)["game_type"]
    assert client.post("/api/v1/game-types", json={"name": "core_game", "version": 1}).status_code == 201
    second_deprecation = _deprecate("core_game", 1)["game_type"]
    replacement = client.post("/api/v1/game-types", json={"name": "core_game", "version": 1}).json()

    def listed(**params):
        response = client.get("/api/v1/game-types", params=params)
        assert response.status_code == 200, response.text
        return [(game_type["id"], game_type["version"], game_type["deprecated_at"]) for game_type in response.json()]

    active_v2 = game_types[1]["id"]
    assert listed(name="core_game") == [(replacement["id"], 1, None), (active_v2, 2, None)]
    assert listed(name="core_game", include_deprecated=True) == [
        (replacement["id"], 1, None),
        (first_deprecation["id"], 1, first_deprecation["deprecated_at"]),
        (second_deprecation["id"], 1, second_deprecation["deprecated_at"]),
        (active_v2, 2, None),
    ]
    assert listed(name="core_game", version=1) == [(replacement["id"], 1, None)]
    assert [entry[0] for entry in listed(name="core_game", version=1, include_deprecated=True)] == [
        replacement["id"],
        first_deprecation["id"],
        second_deprecation["id"],
    ]
    assert listed(name="missing_game", include_deprecated=True) == []
    assert client.get("/api/v1/game-types", params={"version": 1}).status_code == 400


@pytest.mark.parametrize("game_types", [["core_game"]], indirect=True)
def test_deprecated_game_type_is_addressable_by_id_and_by_deprecated_at_but_not_by_name_alone(game_types, test_body_rows):
    deprecated = _deprecate("core_game")["game_type"]

    assert client.get(_path("core_game")).status_code == 404
    assert client.get(f"/api/v1/game-types/by-id/{deprecated['id']}").json() == deprecated
    assert client.get(_path("core_game", deprecated_at=deprecated["deprecated_at"])).json() == deprecated
    assert client.get(_path("core_game", deprecated_at="not-a-timestamp")).status_code == 400

    # The name and version are free again for a new active game type.
    replacement = client.post("/api/v1/game-types", json={"name": "core_game", "version": 1})
    assert replacement.status_code == 201
    assert replacement.json()["id"] != deprecated["id"]
    assert client.get(_path("core_game")).json()["id"] == replacement.json()["id"]


# Deprecation prompts derived game types to rebase


@pytest.mark.parametrize("game_types", [["core_game", {"name": "dm_house_rules", "derived_from": "core_game"}]], indirect=True)
def test_deprecating_an_ancestor_queues_a_rebase_change_for_derived_game_types(game_types, test_body_rows):
    result = _deprecate("core_game")

    [queued] = result["queued_changes"]
    assert queued["derived_game_type"]["name"] == "dm_house_rules"
    assert (queued["resource_kind"], queued["resource_key"], queued["status"]) == ("game_type", "core_game:v1", "pending")

    with SessionLocal() as session:
        change = session.get(GameTypeResourceChange, uuid.UUID(queued["change_id"]))
        snapshot = json.loads(change.snapshot_payload)
        assert snapshot["action"] == "ancestor_deprecated"
        assert snapshot["required_resolution"] == "rebase"
        assert snapshot["ancestor"]["deprecated_at"] == result["game_type"]["deprecated_at"]

    # A derived game type still links its deprecated ancestor, and returns it in full.
    derived = client.get(_path("dm_house_rules")).json()
    assert derived["derived_from"] == result["game_type"]


# Delete


def test_delete_game_type_with_nothing_referencing_it_deletes_it():
    created = client.post("/api/v1/game-types", json={"name": "core_game", "version": 1}).json()

    response = client.delete(_path("core_game"))

    assert response.status_code == 200
    assert response.json()["status"] == "deleted"
    assert client.get(f"/api/v1/game-types/by-id/{created['id']}").status_code == 404


@pytest.mark.parametrize("game_types", [["core_game"]], indirect=True)
def test_delete_game_type_with_history_deprecates_it_with_a_warning(game_types, test_body_rows):
    _add_history_record(game_types[0]["id"])

    response = client.delete(_path("core_game"))

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "deprecated"
    assert body["game_type"]["deprecated_at"] is not None
    assert body["warning"]["warning_type"] == "deprecated_instead_of_deleted"
    [history] = body["warning"]["blocking_resources"]
    assert (history["kind"], history["via"]) == ("resolution", "resolution.game_type_id")


@pytest.mark.parametrize("game_types", [["core_game", {"name": "dm_house_rules", "derived_from": "core_game"}]], indirect=True)
def test_delete_game_type_with_derived_game_types_deprecates_it_and_prompts_them_to_rebase(game_types, test_body_rows):
    response = client.delete(_path("core_game"))

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "deprecated"
    assert [change["derived_game_type"]["name"] for change in body["queued_changes"]] == ["dm_house_rules"]
    assert [(item["kind"], item["name"]) for item in body["warning"]["blocking_resources"]] == [("game_type", "dm_house_rules")]


@pytest.mark.parametrize("game_types", [["core_game"]], indirect=True)
def test_deleting_a_deprecated_game_type_requires_it_to_be_unreferenced(game_types):
    _add_history_record(game_types[0]["id"])
    deprecated_at = client.delete(_path("core_game")).json()["game_type"]["deprecated_at"]

    response = client.delete(_path("core_game", deprecated_at=deprecated_at))
    assert response.status_code == 409
    assert response.json()["warning_type"] == "game_type_in_use"
    assert [item["via"] for item in response.json()["blocking_resources"]] == ["resolution.game_type_id"]

    with SessionLocal() as session:
        session.query(Resolution).delete()
        session.commit()

    response = client.delete(_path("core_game", deprecated_at=deprecated_at))
    assert response.status_code == 200
    assert response.json()["status"] == "deleted"


# History records snapshot what they describe


@pytest.mark.parametrize("game_types", [["core_game"]], indirect=True)
def test_history_survives_deleting_the_config_and_entities_it_describes(game_types, test_body_rows):
    game_type_id = uuid.UUID(game_types[0]["id"])
    with SessionLocal() as session:
        action_type = ActionType(name="strike", game_type_id=game_type_id)
        entity_type = EntityType(name="orc", game_type_id=game_type_id)
        session.add_all([action_type, entity_type])
        session.flush()
        config = ActionResolverConfig(name="strike_root", resolver_key="lifecycle_count_targets", game_type_id=game_type_id, action_type_id=action_type.id, relationship_type="root")
        session.add(config)
        session.flush()
        session.add(EffectCalculationConfig(action_resolver_config_id=config.id, dice_count=2, dice_sides=6, bonus=1, multiplier=1))
        target = Entity(name="Grunk", game_type_id=game_type_id, entity_type_id=entity_type.id)
        session.add(target)
        session.flush()

        resolution_id = run_action_resolution(
            session, game_type_id=game_type_id, action_type_id=action_type.id, participants={"target": [target]}, action_resolver_config_id=config.id
        ).id
        session.commit()

        for row in (target, config.effect_config, config, entity_type, action_type):
            session.delete(row)
            session.commit()

    with SessionLocal() as session:
        resolution = session.get(Resolution, resolution_id)
        assert resolution.action_resolver_config_name == "strike_root"
        assert resolution.action_type_name == "strike"
        assert resolution.config_snapshot["root"] == "strike_root"
        [snapshot_config] = resolution.config_snapshot["action_resolver_configs"]
        assert snapshot_config["effect_config"] == {"kind": "calculation", "dice_count": 2, "dice_sides": 6, "bonus": 1, "multiplier": 1}
        [participant] = resolution.participants
        assert participant.entity_snapshot["name"] == "Grunk"
        assert participant.entity_snapshot["entity_type"]["name"] == "orc"
        [step] = resolution.steps
        assert (step.action_resolver_config_name, step.result) == ("strike_root", {"targets": 1})
