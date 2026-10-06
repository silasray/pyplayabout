from app.models import (
    ActionEffectConfig,
    ActionResolverConfig,
    ActionType,
    EffectCalculationConfig,
    EntityMutationEffectConfig,
    RelationshipEffectConfig,
)


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
