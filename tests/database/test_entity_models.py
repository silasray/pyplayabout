import pytest
from sqlalchemy.exc import IntegrityError

from app.database import SessionLocal
from app.models import (
    Entity,
    EntityClass,
    EntityType,
    GameType,
    GameTypeResourceChange,
    ParticipantType,
    Relationship,
    RelationshipParticipant,
    RelationshipType,
    RelationshipTypeParticipantType,
)


def test_entity_model_exists_and_maps_to_entity_table():
    assert Entity.__tablename__ == "entity"
    assert hasattr(EntityType, "entities")
    assert "name" in Entity.__table__.columns.keys()
    assert "entity_type_id" in Entity.__table__.columns.keys()
    assert "game_type_id" in Entity.__table__.columns.keys()


def test_relationship_tables_exist_and_use_participant_roles():
    assert RelationshipType.__tablename__ == "relationship_type"
    assert Relationship.__tablename__ == "relationship"
    assert RelationshipParticipant.__tablename__ == "relationship_participant"
    assert RelationshipTypeParticipantType.__tablename__ == "relationship_type_participant_type"
    assert "relationship_type_id" in Relationship.__table__.columns.keys()
    assert "participant_type_id" in RelationshipParticipant.__table__.columns.keys()
    assert "entity_id" in RelationshipParticipant.__table__.columns.keys()


def test_game_type_supports_derivation_and_review_cycles():
    parent = GameType(name="core_game", version=1, is_generic=False)
    derived = GameType(name="dm_house_rules", version=1, is_generic=False, derived_from=parent)
    assert derived.derived_from_id == parent.id
    assert GameType.__table__.c.derived_from_id is not None

    review = GameTypeResourceChange(
        derived_game_type_id=derived.id,
        source_game_type_id=parent.id,
        resource_kind="entity_type",
        resource_key="weapon",
        status="pending",
        snapshot_payload='{"name": "weapon", "version": 1}',
    )
    assert review.status == "pending"

    review.status = "rejected"
    assert review.status == "rejected"

    review.status = "superseded"
    assert review.status == "superseded"


def test_relationship_participant_validates_entity_type_for_participant_role():
    with SessionLocal() as session:
        game_type = GameType(name="test_game", version=1, is_generic=False)
        session.add(game_type)
        session.flush()

        character_type = EntityType(name="character", game_type_id=game_type.id)
        sword_type = EntityType(name="sword", game_type_id=game_type.id)
        session.add_all([character_type, sword_type])
        session.flush()

        equipper = ParticipantType(name="equipper", game_type_id=game_type.id)
        equipable = ParticipantType(name="equipable", game_type_id=game_type.id)
        session.add_all([equipper, equipable])
        session.flush()

        relationship_type = RelationshipType(name="equipped", game_type_id=game_type.id)
        session.add(relationship_type)
        session.flush()

        session.add(
            RelationshipTypeParticipantType(
                relationship_type_id=relationship_type.id,
                participant_type_id=equipper.id,
                entity_type_id=character_type.id,
            )
        )
        session.add(
            RelationshipTypeParticipantType(
                relationship_type_id=relationship_type.id,
                participant_type_id=equipable.id,
                entity_type_id=sword_type.id,
            )
        )
        session.flush()

        relationship = Relationship(name="og_equips_sword", relationship_type_id=relationship_type.id, game_type_id=game_type.id)
        og = Entity(name="Og the Barbarian", game_type_id=game_type.id, entity_type_id=character_type.id)
        sword = Entity(name="Dirty Longsword", game_type_id=game_type.id, entity_type_id=sword_type.id)
        session.add_all([relationship, og, sword])
        session.flush()

        valid_participant = RelationshipParticipant(
            relationship_id=relationship.id,
            participant_type_id=equipper.id,
            entity_id=og.id,
        )
        invalid_participant = RelationshipParticipant(
            relationship_id=relationship.id,
            participant_type_id=equipper.id,
            entity_id=sword.id,
        )

        session.add(valid_participant)
        with pytest.raises(ValueError, match="entity type"):
            session.add(invalid_participant)
            session.flush()


def test_relationship_type_allows_conflicting_role_mappings_but_blocks_duplicates():
    with SessionLocal() as session:
        game_type = GameType(name="test_game_conflict", version=1, is_generic=False)
        session.add(game_type)
        session.flush()

        character_type = EntityType(name="character_conflict", game_type_id=game_type.id)
        sword_type = EntityType(name="sword_conflict", game_type_id=game_type.id)
        session.add_all([character_type, sword_type])
        session.flush()

        equipper = ParticipantType(name="equipper_conflict", game_type_id=game_type.id)
        session.add(equipper)
        session.flush()

        relationship_type = RelationshipType(name="equipped_conflict", game_type_id=game_type.id)
        session.add(relationship_type)
        session.flush()

        session.add_all(
            [
                RelationshipTypeParticipantType(
                    relationship_type_id=relationship_type.id,
                    participant_type_id=equipper.id,
                    entity_type_id=character_type.id,
                ),
                RelationshipTypeParticipantType(
                    relationship_type_id=relationship_type.id,
                    participant_type_id=equipper.id,
                    entity_type_id=sword_type.id,
                ),
            ]
        )
        session.flush()

        duplicate = RelationshipTypeParticipantType(
            relationship_type_id=relationship_type.id,
            participant_type_id=equipper.id,
            entity_type_id=character_type.id,
        )
        session.add(duplicate)
        with pytest.raises(Exception):
            session.flush()


def _role_fixture(session):
    game_type = GameType(name="role_game", version=1, is_generic=False)
    session.add(game_type)
    session.flush()
    sword = EntityType(name="sword", game_type_id=game_type.id)
    weapon = EntityClass(name="weapon", game_type_id=game_type.id)
    equippable = ParticipantType(name="equippable", game_type_id=game_type.id)
    equipped = RelationshipType(name="equipped", game_type_id=game_type.id)
    session.add_all([sword, weapon, equippable, equipped])
    session.flush()
    return sword, weapon, equippable, equipped


@pytest.mark.parametrize("bind_type, bind_class", [(True, True), (False, False)], ids=["both", "neither"])
def test_relationship_role_binds_exactly_one_of_entity_type_or_class(bind_type, bind_class):
    with SessionLocal() as session:
        sword, weapon, equippable, equipped = _role_fixture(session)
        session.add(
            RelationshipTypeParticipantType(
                relationship_type_id=equipped.id,
                participant_type_id=equippable.id,
                entity_type_id=sword.id if bind_type else None,
                entity_class_id=weapon.id if bind_class else None,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()


@pytest.mark.parametrize("target", ["entity_type", "entity_class"])
def test_relationship_role_blocks_duplicate_bindings_for_type_and_class(target):
    with SessionLocal() as session:
        sword, weapon, equippable, equipped = _role_fixture(session)
        binding = {"entity_type_id": sword.id} if target == "entity_type" else {"entity_class_id": weapon.id}
        session.add(RelationshipTypeParticipantType(relationship_type_id=equipped.id, participant_type_id=equippable.id, **binding))
        session.flush()

        session.add(RelationshipTypeParticipantType(relationship_type_id=equipped.id, participant_type_id=equippable.id, **binding))
        with pytest.raises(IntegrityError):
            session.flush()
