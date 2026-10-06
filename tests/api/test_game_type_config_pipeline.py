import uuid

from app.config_import_export import export_game_type_config, import_game_type_config
from app.database import SessionLocal
from app.models import (
    ActionResolverConfig,
    ActionType,
    EffectCalculationConfig,
    EntityClass,
    EntityType,
    EntityTypeClass,
    GameType,
    ParticipantType,
    RelationshipType,
    RelationshipTypeParticipantType,
)


def test_game_type_config_export_includes_only_game_specific_schema():
    with SessionLocal() as session:
        game_type = GameType(name="alpha_game", version=1, is_generic=False)
        session.add(game_type)
        session.flush()

        weapon = EntityType(name="weapon", game_type_id=game_type.id)
        armor = EntityType(name="armor", game_type_id=game_type.id)
        martial = EntityClass(name="martial", game_type_id=game_type.id)
        gear = EntityClass(name="gear", game_type_id=game_type.id)
        session.add_all([weapon, armor, martial, gear])
        session.flush()

        session.add_all(
            [
                EntityTypeClass(game_type_id=game_type.id, entity_type_id=weapon.id, entity_class_id=martial.id),
                EntityTypeClass(game_type_id=game_type.id, entity_type_id=armor.id, entity_class_id=gear.id),
                ParticipantType(name="attacker", game_type_id=game_type.id),
                ParticipantType(name="target", game_type_id=game_type.id),
            ]
        )
        session.flush()

        relationship_type = RelationshipType(name="attacks", game_type_id=game_type.id)
        session.add(relationship_type)
        session.flush()

        session.add_all(
            [
                RelationshipTypeParticipantType(
                    relationship_type_id=relationship_type.id,
                    participant_type_id=session.query(ParticipantType).filter_by(name="attacker", game_type_id=game_type.id).one().id,
                    entity_type_id=weapon.id,
                ),
                RelationshipTypeParticipantType(
                    relationship_type_id=relationship_type.id,
                    participant_type_id=session.query(ParticipantType).filter_by(name="target", game_type_id=game_type.id).one().id,
                    entity_type_id=armor.id,
                ),
            ]
        )
        session.flush()

        action_type = ActionType(name="strike", game_type_id=game_type.id)
        session.add(action_type)
        session.flush()

        root_config = ActionResolverConfig(
            name="strike_root",
            resolver_key="example_runtime_resolver",
            game_type_id=game_type.id,
            action_type_id=action_type.id,
            relationship_type="root",
        )
        session.add(root_config)
        session.flush()

        session.add(
            EffectCalculationConfig(
                action_resolver_config_id=root_config.id,
                dice_count=2,
                dice_sides=6,
                bonus=1,
                multiplier=1,
            )
        )
        session.flush()

        payload = export_game_type_config(session, game_type.name, game_type.version)

        assert payload["game_type"]["name"] == "alpha_game"
        assert "entity_types" in payload
        assert "entity_classes" in payload
        assert "relationship_types" in payload
        assert "action_resolver_configs" in payload
        assert any(item["name"] == "strike_root" for item in payload["action_resolver_configs"])


def test_game_type_config_import_creates_a_game_specific_bundle_from_json():
    with SessionLocal() as session:
        source = GameType(name="source_game", version=1, is_generic=False)
        clone_target = GameType(name="cloned_game", version=1, is_generic=False)
        session.add_all([source, clone_target])
        session.flush()

        source_payload = {
            "game_type": {"name": "source_game", "version": 1, "is_generic": False},
            "entity_types": [{"name": "sword"}, {"name": "orc"}],
            "entity_classes": [{"name": "weapon"}, {"name": "creature"}],
            "entity_type_classes": [
                {"entity_type": "sword", "entity_class": "weapon"},
                {"entity_type": "orc", "entity_class": "creature"},
            ],
            "participant_types": [{"name": "attacker"}, {"name": "target"}],
            "relationship_types": [
                {
                    "name": "attacks",
                    "participant_roles": [
                        {"participant_type": "attacker", "entity_type": "orc"},
                        {"participant_type": "target", "entity_type": "sword"},
                    ],
                }
            ],
            "action_types": [{"name": "slash"}],
            "action_resolver_configs": [
                {
                    "name": "slash_root",
                    "resolver_key": "example_runtime_resolver",
                    "action_type": "slash",
                    "relationship_type": "root",
                    "effect_config": {"kind": "calculation", "dice_count": 2, "dice_sides": 6, "bonus": 1, "multiplier": 1},
                }
            ],
        }

        clone = import_game_type_config(session, "cloned_game", 1, source_payload, replace_existing=False)

        assert clone["game_type_name"] == "cloned_game"
        assert clone["entity_types_created"] >= 2
        assert clone["resolver_configs_created"] >= 1
        assert session.query(ActionResolverConfig).filter_by(name="slash_root", game_type_id=uuid.UUID(clone["game_type_id"])).count() == 1
