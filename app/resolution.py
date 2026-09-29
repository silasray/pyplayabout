from datetime import datetime, timezone

from app.config_import_export import SNAPSHOT_FORMAT_VERSION, snapshot_action_resolver_config_tree
from app.models import (
    _ACTION_RESOLVER_REGISTRY,
    ActionResolverConfig,
    GameType,
    ParticipantType,
    Resolution,
    ResolutionParticipant,
    ResolutionStep,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _entity_snapshot(entity) -> dict:
    entity_type = entity.entity_type
    return {
        "id": str(entity.id),
        "name": entity.name,
        "entity_type": {"id": str(entity_type.id), "name": entity_type.name} if entity_type is not None else None,
    }


def run_action_resolution(session, *, game_type_id, action_type_id, participants, action_resolver_config_id):
    """Run an action's root resolver and record the run as history.

    The history records snapshot the config tree and participating entities instead of linking to them, so later
    config or entity changes and deletes neither cascade into nor falsify the record. Only the root resolver runs
    for now; walking the rest of the resolver tree is future work (see docs/action_resolution_design.md).
    """
    if session.get(GameType, game_type_id) is None:
        raise ValueError("game_type_id does not exist")

    config = session.get(ActionResolverConfig, action_resolver_config_id)
    if config is None:
        raise ValueError("action_resolver_config_id does not exist")
    if config.game_type_id != game_type_id:
        raise ValueError("action_resolver_config_id belongs to a different game type")
    if config.action_type_id != action_type_id:
        raise ValueError("action_resolver_config_id is not configured for action_type_id")

    resolver_cls = _ACTION_RESOLVER_REGISTRY.get(config.resolver_key)
    if resolver_cls is None:
        raise KeyError(f"No resolver registered for key '{config.resolver_key}'")

    resolution = Resolution(
        game_type_id=game_type_id,
        action_type_id=config.action_type_id,
        action_type_name=config.action_type.name if config.action_type else None,
        action_resolver_config_id=config.id,
        action_resolver_config_name=config.name,
        config_snapshot=snapshot_action_resolver_config_tree(session, config),
        snapshot_format_version=SNAPSHOT_FORMAT_VERSION,
        status="completed",
        created_at=_utcnow(),
    )
    session.add(resolution)
    session.flush()

    for role_name, items in (participants or {}).items():
        if items is None:
            continue
        try:
            participant_items = list(items)
        except TypeError:
            participant_items = [items]

        participant_type = session.query(ParticipantType).filter_by(name=role_name, game_type_id=game_type_id).first()
        for entity in participant_items:
            if entity is None:
                continue
            session.add(
                ResolutionParticipant(
                    resolution_id=resolution.id,
                    role_name=role_name,
                    participant_type_id=participant_type.id if participant_type is not None else None,
                    entity_id=entity.id,
                    entity_snapshot=_entity_snapshot(entity),
                )
            )

    resolver_config_payload = config.effect_config if config.effect_config is not None else config
    result_value = resolver_cls(resolver_config_payload, participants)()

    session.add(
        ResolutionStep(
            resolution_id=resolution.id,
            order=1,
            action_resolver_config_id=config.id,
            action_resolver_config_name=config.name,
            status="completed",
            result=result_value,
        )
    )
    session.flush()
    return resolution
