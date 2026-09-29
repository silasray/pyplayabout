from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.models import (
    ActionResolverConfig,
    ActionType,
    EffectCalculationConfig,
    EntityClass,
    EntityMutationEffectConfig,
    EntityType,
    EntityTypeClass,
    GameType,
    GameTypeResourceChange,
    ParticipantType,
    RelationshipEffectConfig,
    RelationshipType,
    RelationshipTypeParticipantType,
)


def export_game_type_config(session: Session, game_type_name: str, game_type_version: int) -> dict[str, Any]:
    """Serialize a game type and all of its configuration data into a portable payload.

    This is intentionally a database export, not a migration artifact. It keeps product- or
    game-space data in runtime JSON payloads instead of hard-coded schema migrations.
    """
    game_type = session.query(GameType).filter_by(name=game_type_name, version=int(game_type_version), deprecated_at=None).one_or_none()
    if game_type is None:
        raise ValueError(f"Game type '{game_type_name}' v{game_type_version} does not exist")

    entity_types = [
        {"name": entry.name}
        for entry in session.query(EntityType).filter_by(game_type_id=game_type.id).order_by(EntityType.name).all()
    ]

    entity_classes = [
        {"name": entry.name}
        for entry in session.query(EntityClass).filter_by(game_type_id=game_type.id).order_by(EntityClass.name).all()
    ]

    entity_type_classes = [
        {
            "entity_type": entry.entity_type.name,
            "entity_class": entry.entity_class.name,
        }
        for entry in session.query(EntityTypeClass)
        .filter_by(game_type_id=game_type.id)
        .join(EntityType, EntityTypeClass.entity_type_id == EntityType.id)
        .join(EntityClass, EntityTypeClass.entity_class_id == EntityClass.id)
        .order_by(EntityType.name, EntityClass.name)
        .all()
    ]

    participant_types = [
        {"name": item.name}
        for item in session.query(ParticipantType)
        .filter_by(game_type_id=game_type.id)
        .order_by(ParticipantType.name)
        .all()
    ]

    relationship_types = [
        serialize_relationship_type(relationship_type)
        for relationship_type in session.query(RelationshipType).filter_by(game_type_id=game_type.id).order_by(RelationshipType.name).all()
    ]

    action_types = [
        {"name": item.name}
        for item in session.query(ActionType).filter_by(game_type_id=game_type.id).order_by(ActionType.name).all()
    ]

    action_resolver_configs = [
        serialize_action_resolver_config(config)
        for config in session.query(ActionResolverConfig).filter_by(game_type_id=game_type.id).order_by(ActionResolverConfig.name).all()
    ]

    return {
        "game_type": {
            "name": game_type.name,
            "version": game_type.version,
            "is_generic": game_type.is_generic,
        },
        "entity_types": entity_types,
        "entity_classes": entity_classes,
        "entity_type_classes": entity_type_classes,
        "participant_types": participant_types,
        "relationship_types": relationship_types,
        "action_types": action_types,
        "action_resolver_configs": action_resolver_configs,
    }


SNAPSHOT_FORMAT_VERSION = 1


def serialize_effect_config(effect: Any) -> dict[str, Any] | None:
    if effect is None:
        return None
    if isinstance(effect, EffectCalculationConfig):
        payload: dict[str, Any] = {
            "kind": "calculation",
            "dice_count": effect.dice_count,
            "dice_sides": effect.dice_sides,
            "bonus": effect.bonus,
            "multiplier": effect.multiplier,
        }
        if effect.participants:
            payload["participants"] = sorted(
                (
                    {
                        "participant_type": participant.participant_type.name,
                        **({"entity_class": participant.entity_class.name} if participant.entity_class else {"entity_type": participant.entity_type.name}),
                    }
                    for participant in effect.participants
                ),
                key=lambda item: (item["participant_type"], item.get("entity_type") or item.get("entity_class")),
            )
        return payload
    if isinstance(effect, EntityMutationEffectConfig):
        return {
            "kind": "entity_mutation",
            "mutation_kind": effect.mutation_kind,
            "entity_type": effect.entity_type.name if effect.entity_type else None,
            "entity_class": effect.entity_class.name if effect.entity_class else None,
            "count_dice_count": effect.count_dice_count,
            "count_dice_sides": effect.count_dice_sides,
            "count_bonus": effect.count_bonus,
        }
    if isinstance(effect, RelationshipEffectConfig):
        return {"kind": "relationship", "relationship_type": effect.relationship_type.name if effect.relationship_type else None}
    return {"kind": getattr(effect, "effect_kind", "unknown")}


def serialize_action_resolver_config(config: ActionResolverConfig) -> dict[str, Any]:
    """Serialize one resolver config node in the export format. Used by export, import conflict reports and history snapshots."""
    return {
        "name": config.name,
        "resolver_key": config.resolver_key,
        "relationship_type": config.relationship_type,
        "action_type": config.action_type.name if config.action_type else None,
        "parent": config.parent.name if config.parent else None,
        "effect_config": serialize_effect_config(config.effect_config),
    }


def serialize_relationship_type(relationship_type: RelationshipType) -> dict[str, Any]:
    return {
        "name": relationship_type.name,
        "participant_roles": sorted(
            (_participant_role_payload(row) for row in relationship_type.participant_types),
            key=lambda role: (role["participant_type"], "entity_class" in role, role.get("entity_type") or role.get("entity_class")),
        ),
    }


def snapshot_action_resolver_config_tree(session: Session, root: ActionResolverConfig) -> dict[str, Any]:
    """Serialize a resolver config and all of its descendants, plus the relationship types their effects create, for history records.

    The snapshot uses the export format so it stays readable after the live config changes or is deleted.
    """
    configs: list[ActionResolverConfig] = []
    pending = [root]
    while pending:
        config = pending.pop(0)
        configs.append(config)
        pending.extend(sorted(config.children, key=lambda child: child.name))

    relationship_types = {
        config.effect_config.relationship_type.name: config.effect_config.relationship_type
        for config in configs
        if isinstance(config.effect_config, RelationshipEffectConfig) and config.effect_config.relationship_type is not None
    }
    game_type = session.get(GameType, root.game_type_id)
    return {
        "format_version": SNAPSHOT_FORMAT_VERSION,
        "game_type": {"id": str(game_type.id), "name": game_type.name, "version": game_type.version},
        "root": root.name,
        "action_resolver_configs": [serialize_action_resolver_config(config) for config in configs],
        "relationship_types": [serialize_relationship_type(relationship_types[name]) for name in sorted(relationship_types)],
    }


def _participant_role_payload(row: RelationshipTypeParticipantType) -> dict[str, str]:
    if row.entity_class is not None:
        return {"participant_type": row.participant_type.name, "entity_class": row.entity_class.name}
    return {"participant_type": row.participant_type.name, "entity_type": row.entity_type.name}


def export_game_type_config_gql(session: Session, game_type_name: str, game_type_version: int) -> dict[str, Any]:
    """Compatibility wrapper for GraphQL-style configuration exports."""
    return export_game_type_config(session, game_type_name, game_type_version)


def _queue_derived_game_type_change(
    session: Session,
    derived_game_type: GameType,
    source_game_type: GameType,
    resource_kind: str,
    resource_key: str,
    payload: dict[str, Any],
    source_resource_id: uuid.UUID | None = None,
    derived_resource_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Record a parent-originated change as a pending review item for a derived game type."""
    if not resource_key:
        return {}

    existing = (
        session.query(GameTypeResourceChange)
        .filter_by(
            derived_game_type_id=derived_game_type.id,
            source_game_type_id=source_game_type.id,
            resource_kind=resource_kind,
            resource_key=resource_key,
        )
        .one_or_none()
    )
    if existing is None:
        existing = GameTypeResourceChange(
            derived_game_type_id=derived_game_type.id,
            source_game_type_id=source_game_type.id,
            resource_kind=resource_kind,
            resource_key=resource_key,
            source_resource_id=source_resource_id,
            derived_resource_id=derived_resource_id,
            status="pending",
            snapshot_payload=json.dumps(payload, sort_keys=True),
        )
        session.add(existing)
    else:
        existing.source_resource_id = source_resource_id
        existing.derived_resource_id = derived_resource_id
        existing.status = "pending"
        existing.snapshot_payload = json.dumps(payload, sort_keys=True)

    session.flush()
    return {
        "resource_kind": resource_kind,
        "resource_key": resource_key,
        "status": existing.status,
        "source_game_type": source_game_type.name,
        "derived_game_type": derived_game_type.name,
    }


def _queue_derived_game_type_import(session: Session, game_type: GameType, payload: dict[str, Any]) -> dict[str, Any]:
    if game_type.derived_from_id is None:
        return {"status": "ok", "changes": []}

    source_game_type = session.query(GameType).filter_by(id=game_type.derived_from_id).one_or_none()
    if source_game_type is None:
        raise ValueError(f"Game type '{game_type.name}' v{game_type.version} is derived from a game type that does not exist")

    changes: list[dict[str, Any]] = []
    resource_map = {
        "entity_types": ("entity_type", EntityType, "name"),
        "entity_classes": ("entity_class", EntityClass, "name"),
        "participant_types": ("participant_type", ParticipantType, "name"),
        "relationship_types": ("relationship_type", RelationshipType, "name"),
        "action_types": ("action_type", ActionType, "name"),
        "action_resolver_configs": ("action_resolver_config", ActionResolverConfig, "name"),
    }

    for key, (resource_kind, model, field_name) in resource_map.items():
        for item in payload.get(key, []) or []:
            resource_key = item.get(field_name)
            if not resource_key:
                continue
            source_row = session.query(model).filter(getattr(model, field_name) == resource_key)
            if hasattr(model, "game_type_id"):
                source_row = source_row.filter_by(game_type_id=source_game_type.id)
            source_row = source_row.one_or_none()
            changes.append(
                _queue_derived_game_type_change(
                    session,
                    derived_game_type=game_type,
                    source_game_type=source_game_type,
                    resource_kind=resource_kind,
                    resource_key=str(resource_key),
                    payload=item,
                    source_resource_id=getattr(source_row, "id", None),
                )
            )

    for item in payload.get("entity_type_classes", []) or []:
        entity_type_name = item.get("entity_type")
        entity_class_name = item.get("entity_class")
        if not entity_type_name or not entity_class_name:
            continue
        source_row = (
            session.query(EntityTypeClass)
            .join(EntityType, EntityType.id == EntityTypeClass.entity_type_id)
            .join(EntityClass, EntityClass.id == EntityTypeClass.entity_class_id)
            .filter(EntityType.name == entity_type_name, EntityClass.name == entity_class_name, EntityTypeClass.game_type_id == source_game_type.id)
            .one_or_none()
        )
        changes.append(
            _queue_derived_game_type_change(
                session,
                derived_game_type=game_type,
                source_game_type=source_game_type,
                resource_kind="entity_type_class",
                resource_key=f"{entity_type_name}:{entity_class_name}",
                payload=item,
                source_resource_id=getattr(source_row, "id", None),
            )
        )

    return {"status": "pending_review", "changes": [change for change in changes if change]}


def _role_target(
    role: dict[str, Any],
    entity_type_map: dict[str, EntityType],
    entity_class_map: dict[str, EntityClass],
    context: str,
) -> dict[str, uuid.UUID]:
    """Resolve a participant role's target to exactly one of an entity type or an entity class."""
    entity_type_name = role.get("entity_type")
    entity_class_name = role.get("entity_class")
    if (entity_type_name is None) == (entity_class_name is None):
        raise ValueError(f"{context} role must name exactly one of entity_type or entity_class: {role}")
    if entity_type_name is not None:
        if entity_type_name not in entity_type_map:
            raise ValueError(f"{context} role references unknown entity type '{entity_type_name}'")
        return {"entity_type_id": entity_type_map[entity_type_name].id, "entity_class_id": None}
    if entity_class_name not in entity_class_map:
        raise ValueError(f"{context} role references unknown entity class '{entity_class_name}'")
    return {"entity_type_id": None, "entity_class_id": entity_class_map[entity_class_name].id}


def _coerce_uuid(value: Any) -> uuid.UUID | Any:
    if value is None:
        return value
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str):
        return uuid.UUID(value)
    return value


def import_game_type_config(session: Session, game_type_name: str, game_type_version: int, payload: dict[str, Any], replace_existing: bool = False) -> dict[str, Any]:
    """Bulk import a serialized game-type config bundle into an existing game type.

    The game type itself must already exist; this never creates it.
    """
    if not isinstance(payload, dict):
        raise TypeError("Import payload must be a dictionary")

    game_type = session.query(GameType).filter_by(name=game_type_name, version=int(game_type_version), deprecated_at=None).one_or_none()
    if game_type is None:
        raise ValueError(f"Game type '{game_type_name}' v{game_type_version} does not exist")
    if game_type.derived_from_id is not None:
        return _queue_derived_game_type_import(session, game_type, payload)
    if replace_existing:
        raise NotImplementedError("replace_existing=True is not implemented for bulk config imports")

    entity_type_map: dict[str, EntityType] = {}
    for item in payload.get("entity_types", []):
        name = item.get("name")
        if not name:
            continue
        entity_type = session.query(EntityType).filter_by(name=name, game_type_id=game_type.id).one_or_none()
        if entity_type is None:
            entity_type = EntityType(name=name, game_type_id=game_type.id)
            session.add(entity_type)
            session.flush()
        entity_type_map[name] = entity_type

    entity_class_map: dict[str, EntityClass] = {}
    for item in payload.get("entity_classes", []):
        name = item.get("name")
        if not name:
            continue
        entity_class = session.query(EntityClass).filter_by(name=name, game_type_id=game_type.id).one_or_none()
        if entity_class is None:
            entity_class = EntityClass(name=name, game_type_id=game_type.id)
            session.add(entity_class)
            session.flush()
        entity_class_map[name] = entity_class

    for item in payload.get("entity_type_classes", []):
        source_type = entity_type_map.get(item.get("entity_type"))
        source_class = entity_class_map.get(item.get("entity_class"))
        if source_type is None or source_class is None:
            continue
        has_link = (
            session.query(EntityTypeClass)
            .filter_by(game_type_id=game_type.id, entity_type_id=source_type.id, entity_class_id=source_class.id)
            .one_or_none()
        )
        if has_link is None:
            session.add(
                EntityTypeClass(
                    game_type_id=game_type.id,
                    entity_type_id=source_type.id,
                    entity_class_id=source_class.id,
                )
            )

    participant_type_map: dict[str, ParticipantType] = {}
    for item in payload.get("participant_types", []):
        name = item.get("name")
        if not name:
            continue
        participant_type = session.query(ParticipantType).filter_by(name=name, game_type_id=game_type.id).one_or_none()
        if participant_type is None:
            participant_type = ParticipantType(name=name, game_type_id=game_type.id)
            session.add(participant_type)
            session.flush()
        participant_type_map[name] = participant_type

    action_type_map: dict[str, ActionType] = {}
    for item in payload.get("action_types", []):
        name = item.get("name")
        if not name:
            continue
        action_type = session.query(ActionType).filter_by(name=name, game_type_id=game_type.id).one_or_none()
        if action_type is None:
            action_type = ActionType(name=name, game_type_id=game_type.id)
            session.add(action_type)
            session.flush()
        action_type_map[name] = action_type

    relationship_type_map: dict[str, RelationshipType] = {}
    for item in payload.get("relationship_types", []):
        name = item.get("name")
        if not name:
            continue
        relationship_type = session.query(RelationshipType).filter_by(name=name, game_type_id=game_type.id).one_or_none()
        if relationship_type is None:
            relationship_type = RelationshipType(name=name, game_type_id=game_type.id)
            session.add(relationship_type)
            session.flush()
        relationship_type_map[name] = relationship_type

        for role in item.get("participant_roles", []):
            participant_name = role.get("participant_type")
            participant_type = participant_type_map.get(participant_name)
            if participant_type is None:
                raise ValueError(f"Relationship type '{name}' role references unknown participant type '{participant_name}'")
            target = _role_target(role, entity_type_map, entity_class_map, f"Relationship type '{name}'")
            existing_link = (
                session.query(RelationshipTypeParticipantType)
                .filter_by(relationship_type_id=relationship_type.id, participant_type_id=participant_type.id, **target)
                .one_or_none()
            )
            if existing_link is None:
                session.add(
                    RelationshipTypeParticipantType(
                        relationship_type_id=relationship_type.id,
                        participant_type_id=participant_type.id,
                        **target,
                    )
                )

    for item in payload.get("action_resolver_configs", []):
        name = item.get("name")
        if not name:
            continue

        action_type_name = item.get("action_type")
        action_type = action_type_map.get(action_type_name)
        if action_type is None:
            action_type = session.query(ActionType).filter_by(name=action_type_name, game_type_id=game_type.id).one_or_none()
            if action_type is None and action_type_name:
                action_type = ActionType(name=action_type_name, game_type_id=game_type.id)
                session.add(action_type)
                session.flush()
            if action_type is not None:
                action_type_map[action_type_name] = action_type

        resolver_key = item.get("resolver_key") or name
        config = session.query(ActionResolverConfig).filter_by(name=name, game_type_id=game_type.id).one_or_none()
        if config is None:
            config = ActionResolverConfig(
                name=name,
                resolver_key=resolver_key,
                game_type_id=game_type.id,
                action_type_id=action_type.id if action_type else None,
                relationship_type=item.get("relationship_type", "child"),
            )
            session.add(config)
            session.flush()
        else:
            config.game_type_id = game_type.id
            config.action_type_id = action_type.id if action_type else config.action_type_id
            config.resolver_key = resolver_key
            config.relationship_type = item.get("relationship_type", config.relationship_type)

        effect_config = item.get("effect_config") or {}
        kind = str(effect_config.get("kind") or effect_config.get("effect_kind") or "basic").lower()

        if kind == "calculation":
            effect = EffectCalculationConfig(
                action_resolver_config_id=config.id,
                dice_count=int(effect_config.get("dice_count", 0)),
                dice_sides=int(effect_config.get("dice_sides", 0)),
                bonus=int(effect_config.get("bonus", 0)),
                multiplier=int(effect_config.get("multiplier", 1)),
            )
            session.add(effect)
        elif kind == "entity_mutation":
            entity_type = entity_type_map.get(effect_config.get("entity_type"))
            entity_class = entity_class_map.get(effect_config.get("entity_class"))
            effect = EntityMutationEffectConfig(
                action_resolver_config_id=config.id,
                mutation_kind=str(effect_config.get("mutation_kind") or kind),
                entity_type_id=entity_type.id if entity_type else None,
                entity_class_id=entity_class.id if entity_class else None,
                count_dice_count=effect_config.get("count_dice_count"),
                count_dice_sides=effect_config.get("count_dice_sides"),
                count_bonus=effect_config.get("count_bonus", 0),
            )
            session.add(effect)
        elif kind == "relationship":
            relationship_name = effect_config.get("relationship_type")
            relationship_type = relationship_type_map.get(relationship_name)
            if relationship_type is None:
                relationship_type = session.query(RelationshipType).filter_by(name=relationship_name, game_type_id=game_type.id).one_or_none()
            effect = RelationshipEffectConfig(
                action_resolver_config_id=config.id,
                relationship_type_id=relationship_type.id if relationship_type else None,
            )
            session.add(effect)
        else:
            continue

    session.flush()
    for item in payload.get("action_resolver_configs", []):
        parent_name = item.get("parent")
        if not item.get("name") or not parent_name:
            continue
        parent = session.query(ActionResolverConfig).filter_by(name=parent_name, game_type_id=game_type.id).one_or_none()
        if parent is None:
            raise ValueError(f"Action resolver config '{item['name']}' references unknown parent '{parent_name}'")
        session.query(ActionResolverConfig).filter_by(name=item["name"], game_type_id=game_type.id).one().parent_id = parent.id

    session.flush()
    return {
        "game_type_name": game_type.name,
        "game_type_id": str(game_type.id),
        "game_type_uuid": _coerce_uuid(game_type.id),
        "entity_types_created": len(payload.get("entity_types", [])),
        "entity_classes_created": len(payload.get("entity_classes", [])),
        "resolver_configs_created": len(payload.get("action_resolver_configs", [])),
    }


def import_game_type_config_gql(session: Session, game_type_name: str, game_type_version: int, payload: dict[str, Any], replace_existing: bool = False) -> dict[str, Any]:
    """Compatibility wrapper for GraphQL-style configuration imports."""
    return import_game_type_config(session, game_type_name, game_type_version, payload, replace_existing=replace_existing)
