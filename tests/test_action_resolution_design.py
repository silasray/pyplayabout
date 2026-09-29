from app.database import Base, SessionLocal, engine
from app.models import (
    ActionEffectConfig,
    ActionResolverConfig,
    ActionType,
    BaseActionResolver,
    EffectCalculationConfig,
    Entity,
    EntityMutationEffectConfig,
    EntityType,
    GameType,
    ParticipantType,
    RelationshipEffectConfig,
    Resolution,
    ResolutionParticipant,
    ResolutionStep,
)
from app.resolution import run_action_resolution


class ExampleRuntimeResolver(BaseActionResolver):
    __resolver_key__ = "example_runtime_resolver"

    def __call__(self):
        return {"resolved": len(self.participants.get("target", []))}


def reset_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


reset_db()


def test_action_resolver_config_supports_parent_child_chains():
    assert ActionResolverConfig.__tablename__ == "action_resolver_config"
    assert "parent_id" in ActionResolverConfig.__table__.columns.keys()
    assert "action_type_id" in ActionResolverConfig.__table__.columns.keys()
    assert hasattr(ActionResolverConfig, "children")
    assert hasattr(ActionResolverConfig, "parent")


def test_effect_configs_are_specialized_subtypes():
    assert ActionEffectConfig.__tablename__ == "action_effect_config"
    assert EffectCalculationConfig.__tablename__ == "effect_calculation_config"
    assert EntityMutationEffectConfig.__tablename__ == "entity_mutation_effect_config"
    assert RelationshipEffectConfig.__tablename__ == "relationship_effect_config"
    assert "action_resolver_config_id" in ActionEffectConfig.__table__.columns.keys()
    assert "effect_kind" in ActionEffectConfig.__table__.columns.keys()
    assert hasattr(ActionResolverConfig, "effect_config")


def test_action_type_can_have_root_action_resolver_config():
    assert ActionType.__table__ is not None
    assert ActionResolverConfig.__table__ is not None


def test_runtime_resolution_records_are_created_and_resolver_runs():
    with SessionLocal() as session:
        game_type = GameType(name="runtime_game", version=1, is_generic=False)
        session.add(game_type)
        session.flush()

        action_type = ActionType(name="runtime_action", game_type_id=game_type.id)
        entity_type = EntityType(name="runtime_entity", game_type_id=game_type.id)
        session.add_all([action_type, entity_type])
        session.flush()

        participant_type = ParticipantType(name="target", game_type_id=game_type.id)
        session.add(participant_type)
        session.flush()

        root_config = ActionResolverConfig(
            name="runtime_root",
            resolver_key="example_runtime_resolver",
            game_type_id=game_type.id,
            action_type_id=action_type.id,
            relationship_type="root",
        )
        session.add(root_config)
        session.flush()

        effect_config = EffectCalculationConfig(
            action_resolver_config_id=root_config.id,
            dice_count=1,
            dice_sides=6,
            bonus=0,
            multiplier=1,
        )
        session.add(effect_config)
        session.flush()

        entity = Entity(name="runtime_target", game_type_id=game_type.id, entity_type_id=entity_type.id)
        session.add(entity)
        session.flush()

        resolution = run_action_resolution(
            session,
            game_type_id=game_type.id,
            action_type_id=action_type.id,
            participants={"target": [entity]},
            action_resolver_config_id=root_config.id,
        )

        assert isinstance(resolution, Resolution)
        assert resolution.status == "completed"
        assert resolution.steps
        assert isinstance(resolution.steps[0], ResolutionStep)
        assert resolution.participants
        assert isinstance(resolution.participants[0], ResolutionParticipant)
        assert resolution.steps[0].result == {"resolved": 1}
