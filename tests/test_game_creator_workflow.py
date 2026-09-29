"""End-to-end game creator workflow: build an "equip" action in a generic game type, then copy it into a game."""

import pytest
from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models import (
    ActionResolverConfig,
    ActionType,
    Entity,
    EntityType,
    GameType,
    Relationship,
    RelationshipEffectConfig,
    RelationshipParticipant,
    RelationshipType,
)

client = TestClient(app)

GENERIC = {"name": "test_generic_game_type", "version": 1}
TARGET = {"name": "test_game_type", "version": 1}
EQUIP_BUNDLE = "tests/fixtures/bulk_game_types/equip_generic_bundle.json"

pytestmark = pytest.mark.parametrize(
    "game_types, bulk_game_type_bundle",
    [pytest.param([{**GENERIC, "is_generic": True}, TARGET], [EQUIP_BUNDLE], id="equip")],
    indirect=True,
)

EQUIP_TREE_COPY_REQUEST = {
    "source_game_type": GENERIC,
    "entity_types": [{"name": "character"}, {"name": "sword"}, {"name": "bauble"}],
    "entity_classes": [{"name": "weapon"}],
    "entity_type_classes": [{"entity_type": "sword", "entity_class": "weapon"}],
    "participant_types": [{"name": "equipper"}, {"name": "equippable"}],
    "relationship_types": [{"name": "equipped"}],
    "action_types": [{"name": "equip"}],
    "action_resolver_configs": [{"name": "equip_root"}],
}


def _url(game_type, path):
    return f"/api/v1/game-types/{game_type['name']}/versions/{game_type['version']}/config/{path}"


def _export_without_game_type(game_type):
    response = client.post(_url(game_type, "export"))
    assert response.status_code == 200, response.text
    return {key: value for key, value in response.json().items() if key != "game_type"}


def _copy_equip_tree():
    response = client.post(_url(TARGET, "copy"), json=EQUIP_TREE_COPY_REQUEST)
    assert response.status_code == 200, response.text
    return response.json()


def test_copying_equip_tree_reproduces_generic_configuration(game_types, bulk_game_type_bundle, test_body_rows):
    assert _export_without_game_type(TARGET)["action_resolver_configs"] == []

    copied = _copy_equip_tree()["copied"]
    assert copied["relationship_types"] == ["equipped"]
    assert copied["action_resolver_configs"] == ["equip_root"]

    target_config = _export_without_game_type(TARGET)
    assert target_config == _export_without_game_type(GENERIC)
    assert target_config["relationship_types"] == [
        {
            "name": "equipped",
            "participant_roles": [
                {"participant_type": "equippable", "entity_type": "bauble"},
                {"participant_type": "equippable", "entity_class": "weapon"},
                {"participant_type": "equipper", "entity_type": "character"},
            ],
        }
    ]
    assert target_config["action_resolver_configs"] == [
        {
            "name": "equip_root",
            "resolver_key": "create_relationship",
            "relationship_type": "root",
            "action_type": "equip",
            "parent": None,
            "effect_config": {"kind": "relationship", "relationship_type": "equipped"},
        }
    ]


def test_copied_equip_tree_references_only_target_game_type_resources(game_types, bulk_game_type_bundle, test_body_rows):
    _copy_equip_tree()

    with SessionLocal() as session:
        target = session.query(GameType).filter_by(**TARGET).one()
        equip = session.query(ActionType).filter_by(name="equip", game_type_id=target.id).one()
        equip_root = session.query(ActionResolverConfig).filter_by(action_type_id=equip.id).one()
        assert equip_root.name == "equip_root"
        assert equip_root.game_type_id == target.id

        effect = equip_root.effect_config
        assert isinstance(effect, RelationshipEffectConfig)
        assert effect.relationship_type.game_type_id == target.id

        for role in effect.relationship_type.participant_types:
            assert role.participant_type.game_type_id == target.id
            bound = role.entity_type or role.entity_class
            assert bound.game_type_id == target.id


def test_copied_equipped_relationship_admits_weapon_class_members_and_baubles(game_types, bulk_game_type_bundle, test_body_rows):
    _copy_equip_tree()

    with SessionLocal() as session:
        target = session.query(GameType).filter_by(**TARGET).one()
        equipped = session.query(RelationshipType).filter_by(name="equipped", game_type_id=target.id).one()
        roles = {role.participant_type.name: role.participant_type for role in equipped.participant_types}
        entity_types = {entity_type.name: entity_type for entity_type in session.query(EntityType).filter_by(game_type_id=target.id)}

        def entity(name, type_name):
            row = Entity(name=name, game_type_id=target.id, entity_type_id=entity_types[type_name].id)
            session.add(row)
            session.flush()
            return row

        def equip(equipper, equippable):
            relationship = Relationship(name=f"{equipper.name} equips {equippable.name}", relationship_type_id=equipped.id, game_type_id=target.id)
            session.add(relationship)
            session.flush()
            session.add_all(
                [
                    RelationshipParticipant(relationship_id=relationship.id, participant_type_id=roles["equipper"].id, entity_id=equipper.id),
                    RelationshipParticipant(relationship_id=relationship.id, participant_type_id=roles["equippable"].id, entity_id=equippable.id),
                ]
            )
            session.flush()

        hero = entity("Og the Barbarian", "character")
        equip(hero, entity("Dirty Longsword", "sword"))  # admitted through the weapon class
        equip(hero, entity("Lucky Charm", "bauble"))  # admitted by entity type
        session.commit()

        with pytest.raises(ValueError, match="does not match the allowed participant type or class"):
            equip(hero, entity("Sidekick", "character"))
        session.rollback()


def test_copy_without_listed_dependencies_changes_nothing(game_types, bulk_game_type_bundle, test_body_rows):
    before = _export_without_game_type(TARGET)

    response = client.post(
        _url(TARGET, "copy"),
        json={"source_game_type": GENERIC, "relationship_types": [{"name": "equipped"}], "action_types": [{"name": "equip"}], "action_resolver_configs": [{"name": "equip_root"}]},
    )

    assert response.status_code == 409
    body = response.json()
    assert body["warning_type"] == "missing_copy_dependencies"
    assert {(item["kind"], item["name"]) for item in body["blocking_resources"]} == {
        ("participant_type", "equipper"),
        ("participant_type", "equippable"),
        ("entity_type", "character"),
        ("entity_type", "bauble"),
        ("entity_class", "weapon"),
    }
    for item in body["blocking_resources"]:
        assert item["required_by"] == [{"kind": "relationship_type", "name": "equipped"}]
        assert item["game_type"]["name"] == TARGET["name"]
    assert _export_without_game_type(TARGET) == before
