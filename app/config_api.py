from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config_import_export import export_game_type_config, import_game_type_config, serialize_action_resolver_config
from app.database import Base, get_db
from app.models import (
    ActionResolverConfig,
    ActionType,
    EffectCalculationConfig,
    EffectCalculationParticipant,
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

router = APIRouter(prefix="/api/v1/game-types", tags=["game-types"])


class WarningResponse(BaseModel):
    """Body of every 409 response: what blocked the request and why, so it can be revised and resubmitted.

    Each entry in ``blocking_resources`` describes one blocker. Database rows are described by ``kind``
    (table name), ``id``, ``name`` and ``game_type`` where they have them, their remaining ``fields``, and a
    ``reason``; entries may add context such as ``dependents``, ``incoming`` or ``required_by``.
    """

    model_config = ConfigDict(extra="forbid")
    warning_type: str
    message: str
    confirmation_token: str | None = None
    affected_game_types: list[str] = Field(default_factory=list)
    blocking_resources: list[dict[str, Any]] = Field(min_length=1)
    details: dict[str, Any] | None = None


class BulkConfigImportPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    game_type: dict[str, Any] | None = None
    entity_types: list[dict[str, Any]] | None = None
    entity_classes: list[dict[str, Any]] | None = None
    entity_type_classes: list[dict[str, Any]] | None = None
    participant_types: list[dict[str, Any]] | None = None
    relationship_types: list[dict[str, Any]] | None = None
    action_types: list[dict[str, Any]] | None = None
    action_resolver_configs: list[dict[str, Any]] | None = None


class BulkDeletePreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_types: list[dict[str, Any]] | None = None
    entity_classes: list[dict[str, Any]] | None = None
    participant_types: list[dict[str, Any]] | None = None
    relationship_types: list[dict[str, Any]] | None = None
    action_types: list[dict[str, Any]] | None = None
    action_resolver_configs: list[dict[str, Any]] | None = None


class BulkConfigConfirmationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmation_token: str
    accept: bool = False


class GameTypeReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    version: int


class BulkCopyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_game_type: GameTypeReference
    entity_types: list[dict[str, Any]] | None = None
    entity_classes: list[dict[str, Any]] | None = None
    entity_type_classes: list[dict[str, Any]] | None = None
    participant_types: list[dict[str, Any]] | None = None
    relationship_types: list[dict[str, Any]] | None = None
    action_types: list[dict[str, Any]] | None = None
    action_resolver_configs: list[dict[str, Any]] | None = None


class BulkDeleteConfirmationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmation_token: str
    accept: bool = False


class GameTypeCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=255)
    version: int = Field(default=1, ge=1)
    is_generic: bool = False
    derived_from: GameTypeReference | None = None


def _game_type_response(game_type: GameType) -> dict[str, Any]:
    """Full representation of a game type. A linked parent is always included in full, even when it is deprecated."""
    parent = game_type.derived_from
    return {
        "id": str(game_type.id),
        "name": game_type.name,
        "version": game_type.version,
        "is_generic": game_type.is_generic,
        "deprecated_at": _jsonable(game_type.deprecated_at),
        "derived_from": _game_type_response(parent) if parent is not None else None,
    }


_CONFIG_MODELS: dict[str, tuple[type, str]] = {
    "entity_types": (EntityType, "entity type"),
    "entity_classes": (EntityClass, "entity class"),
    "participant_types": (ParticipantType, "participant type"),
    "relationship_types": (RelationshipType, "relationship type"),
    "action_types": (ActionType, "action type"),
    "action_resolver_configs": (ActionResolverConfig, "action resolver config"),
}
_CASCADING_DELETE_CATEGORIES = {"relationship_types", "action_resolver_configs", "action_types"}


def _jsonable(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _game_type_summary(session: Session, game_type_id: uuid.UUID) -> dict[str, Any]:
    game_type = session.get(GameType, game_type_id)
    if game_type is None:
        return {"id": str(game_type_id), "exists": False}
    return {"id": str(game_type.id), "name": game_type.name, "version": game_type.version}


def _describe_row(session: Session, table: Any, values: Mapping[str, Any], **extra: Any) -> dict[str, Any]:
    primary_key = next(iter(table.primary_key.columns)).name
    description: dict[str, Any] = {"kind": table.name, "id": _jsonable(values[primary_key])}
    if "name" in values:
        description["name"] = values["name"]
    if values.get("game_type_id") is not None:
        description["game_type"] = _game_type_summary(session, values["game_type_id"])
    description["fields"] = {key: _jsonable(value) for key, value in values.items() if key not in {primary_key, "name", "game_type_id"}}
    description.update(extra)
    return description


def _describe(session: Session, row: Any, **extra: Any) -> dict[str, Any]:
    table = row.__table__
    return _describe_row(session, table, {column.name: getattr(row, column.key) for column in table.columns}, **extra)


def _referencing_rows(session: Session, table: Any, row_id: uuid.UUID) -> list[dict[str, Any]]:
    """Describe every row with a foreign key to the given row, excluding joined-inheritance subtype rows."""
    primary_key = next(iter(table.primary_key.columns)).name
    rows = []
    for other in Base.metadata.sorted_tables:
        for fk in other.foreign_keys:
            if fk.column.table is not table or fk.column.name != primary_key or fk.parent.primary_key:
                continue
            for values in session.execute(select(other).where(fk.parent == row_id)).mappings():
                rows.append(_describe_row(session, other, values, via=f"{other.name}.{fk.parent.name}"))
    return rows


def _conflict(
    warning_type: str,
    message: str,
    blocking_resources: list[dict[str, Any]],
    affected_game_types: list[str],
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    warning = WarningResponse(
        warning_type=warning_type,
        message=message,
        affected_game_types=affected_game_types,
        blocking_resources=blocking_resources,
        details=details,
    )
    return JSONResponse(status_code=status.HTTP_409_CONFLICT, content=warning.model_dump(mode="json"))


def _game_type_for_request(session: Session, game_type_name: str, game_type_version: int) -> GameType:
    game_type = session.query(GameType).filter_by(name=game_type_name, version=game_type_version, deprecated_at=None).one_or_none()
    if game_type is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Game type '{game_type_name}' v{game_type_version} does not exist",
        )
    return game_type


def _payload_game_type_mismatches(payload: dict[str, Any], game_type: GameType) -> list[dict[str, Any]]:
    mismatches = []
    game_definition = payload.get("game_type") or {}
    target_name = game_definition.get("name")
    if target_name and target_name != game_type.name:
        mismatches.append(
            {
                "kind": "payload_game_type",
                "location": "game_type.name",
                "value": target_name,
                "expected": game_type.name,
                "reason": f"payload targets game type '{target_name}', but this import is scoped to '{game_type.name}'",
            }
        )
    target_version = game_definition.get("version")
    if target_version is not None and int(target_version) != game_type.version:
        mismatches.append(
            {
                "kind": "payload_game_type",
                "location": "game_type.version",
                "value": target_version,
                "expected": game_type.version,
                "reason": f"payload targets v{target_version}, but this import is scoped to v{game_type.version}",
            }
        )

    for key in ["entity_types", "entity_classes", "entity_type_classes", "participant_types", "relationship_types", "action_types", "action_resolver_configs"]:
        for index, item in enumerate(payload.get(key, []) or []):
            if isinstance(item, dict) and item.get("game_type") and item["game_type"] != game_type.name:
                mismatches.append(
                    {
                        "kind": key,
                        "location": f"{key}[{index}]",
                        "name": item.get("name"),
                        "value": item["game_type"],
                        "expected": game_type.name,
                        "reason": f"payload item is associated with game type '{item['game_type']}', not '{game_type.name}'",
                    }
                )
    return mismatches


def _import_collisions(session: Session, game_type: GameType, payload: dict[str, Any]) -> list[dict[str, Any]]:
    collisions = []
    for category, (model, label) in _CONFIG_MODELS.items():
        incoming_by_name = {item["name"]: item for item in payload.get(category, []) or [] if isinstance(item, dict) and item.get("name")}
        if not incoming_by_name:
            continue
        existing_rows = session.query(model).filter(model.name.in_(incoming_by_name), model.game_type_id == game_type.id).order_by(model.name).all()
        for existing in existing_rows:
            collision = _describe(
                session,
                existing,
                reason=f"{label} '{existing.name}' already exists in this game type and would be replaced if imported without confirmation",
                incoming=incoming_by_name[existing.name],
            )
            if model is ActionResolverConfig:
                current = serialize_action_resolver_config(existing)
                collision["current"] = current
                collision["changed"] = incoming_by_name[existing.name] != current
            collisions.append(collision)
    return collisions


def _import_conflict(session: Session, game_type: GameType, payload: dict[str, Any]) -> JSONResponse | None:
    mismatches = _payload_game_type_mismatches(payload, game_type)
    if mismatches:
        affected = sorted({game_type.name, *(str(item["value"]) for item in mismatches if item["location"] != "game_type.version")})
        return _conflict(
            "game_type_mismatch",
            f"{len(mismatches)} part(s) of the payload target a different game type than {_game_type_label(game_type)}; correct them and resubmit.",
            mismatches,
            affected,
        )

    collisions = _import_collisions(session, game_type, payload)
    if collisions:
        by_category: dict[str, list[str]] = {}
        for collision in collisions:
            by_category.setdefault(collision["kind"], []).append(collision["name"])
        return _conflict(
            "key_collision",
            f"{len(collisions)} resource(s) in the payload already exist in {_game_type_label(game_type)}; remove or rename them and resubmit.",
            collisions,
            [game_type.name],
            details={"conflicts_by_kind": by_category},
        )
    return None


def _delete_conflict(session: Session, game_type: GameType, payload: dict[str, Any]) -> JSONResponse | None:
    risks = []
    for category in sorted(_CASCADING_DELETE_CATEGORIES):
        model, label = _CONFIG_MODELS[category]
        names = [item.get("name") for item in payload.get(category, []) or [] if item.get("name")]
        if not names:
            continue
        for row in session.query(model).filter(model.name.in_(names), model.game_type_id == game_type.id).order_by(model.name).all():
            dependents = _referencing_rows(session, model.__table__, row.id)
            risks.append(
                _describe(
                    session,
                    row,
                    reason=f"deleting {label} '{row.name}' may cascade into the {len(dependents)} resource(s) that depend on it and requires explicit confirmation",
                    dependents=dependents,
                )
            )
    if not risks:
        return None
    return _conflict(
        "cascade_warning",
        f"{len(risks)} resource(s) in this delete request may cascade into related game resources; review their dependents before resubmitting.",
        risks,
        [game_type.name],
    )


def _import_or_400(db: Session, game_type: GameType, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return import_game_type_config(db, game_type.name, game_type.version, payload, replace_existing=False)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


def _copy_target_game_type_resource_names(items: list[dict[str, Any]] | None) -> list[str]:
    return [item.get("name") for item in (items or []) if isinstance(item, dict) and item.get("name")]


# Game type lifecycle. See docs/game_type_lifecycle.md for the deprecated state and how it is addressed.

# Derivation depth is limited in the create flow only, pending a design for ownership and permissions across deeper
# ancestor chains. Nothing else should assume this limit: ancestor/descendant handling walks chains of any depth.
MAX_DERIVATION_DEPTH = 1

# Rows in these tables are history: they must outlive the config they describe, so a game type they reference is
# deprecated instead of deleted.
_HISTORY_TABLES = {"resolution", "game_type_resource_change"}
_DERIVED_FROM_REFERENCE = f"{GameType.__tablename__}.derived_from_id"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _ancestors(game_type: GameType) -> list[GameType]:
    ancestors, seen = [], {game_type.id}
    parent = game_type.derived_from
    while parent is not None and parent.id not in seen:
        ancestors.append(parent)
        seen.add(parent.id)
        parent = parent.derived_from
    return ancestors


def _active_descendants(db: Session, game_type: GameType) -> list[GameType]:
    descendants, seen, frontier = [], {game_type.id}, [game_type.id]
    while frontier:
        children = db.query(GameType).filter(GameType.derived_from_id.in_(frontier)).order_by(GameType.name, GameType.version).all()
        frontier = []
        for child in children:
            if child.id in seen:
                continue
            seen.add(child.id)
            frontier.append(child.id)
            if not child.is_deprecated:
                descendants.append(child)
    return descendants


def _deprecate_game_type(db: Session, game_type: GameType) -> list[dict[str, Any]]:
    """Mark a game type deprecated and queue an ancestor-deprecated change for review on every active descendant.

    Derived game types represent rulesets for games in progress, so an ancestor going away must not change their rules
    without acknowledgement; it goes through the same review flow as any other ancestor change.

    TODO: accepting an ancestor-deprecated change needs a "rebase" feature that swaps a new game type into the derived
    game type's ancestry as a whole, rather than copying resources over piece by piece. Until it exists, accepting or
    rejecting the change only records the acknowledgement.
    """
    game_type.deprecated_at = _utcnow()
    db.flush()

    ancestor = {
        "id": str(game_type.id),
        "name": game_type.name,
        "version": game_type.version,
        "deprecated_at": _jsonable(game_type.deprecated_at),
    }
    queued = []
    for descendant in _active_descendants(db, game_type):
        change = GameTypeResourceChange(
            derived_game_type_id=descendant.id,
            source_game_type_id=game_type.id,
            resource_kind=GameType.__tablename__,
            resource_key=f"{game_type.name}:v{game_type.version}",
            source_resource_id=game_type.id,
            status="pending",
            snapshot_payload=json.dumps({"action": "ancestor_deprecated", "ancestor": ancestor, "required_resolution": "rebase"}, sort_keys=True),
        )
        db.add(change)
        db.flush()
        queued.append(
            {
                "change_id": str(change.id),
                "derived_game_type": {"id": str(descendant.id), "name": descendant.name, "version": descendant.version},
                "resource_kind": change.resource_kind,
                "resource_key": change.resource_key,
                "status": change.status,
            }
        )
    return queued


def _parse_deprecated_at(deprecated_at: str) -> datetime:
    try:
        return datetime.fromisoformat(deprecated_at)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"'{deprecated_at}' is not an ISO 8601 deprecated_at timestamp") from exc


def _deprecated_game_type_for_request(db: Session, game_type_name: str, version: int, deprecated_at: str) -> GameType:
    game_type = db.query(GameType).filter_by(name=game_type_name, version=version, deprecated_at=_parse_deprecated_at(deprecated_at)).one_or_none()
    if game_type is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Deprecated game type '{game_type_name}' v{version} deprecated at {deprecated_at} does not exist",
        )
    return game_type


@router.get("")
def list_game_types_endpoint(name: str | None = None, version: int | None = None, include_deprecated: bool = False, db: Session = Depends(get_db)):
    """List game types, optionally filtered by name, or by name and version. Deprecated game types are listed only on request."""
    if version is not None and name is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Filtering by version requires a name")

    query = db.query(GameType)
    if name is not None:
        query = query.filter(GameType.name == name)
    if version is not None:
        query = query.filter(GameType.version == version)
    if not include_deprecated:
        query = query.filter(GameType.deprecated_at.is_(None))
    # Active before deprecated within each name and version, then oldest deprecation first, on every database.
    ordering = (GameType.name, GameType.version, GameType.deprecated_at.is_not(None), GameType.deprecated_at)
    return [_game_type_response(game_type) for game_type in query.order_by(*ordering).all()]


@router.get("/by-id/{game_type_id}")
def get_game_type_by_id_endpoint(game_type_id: uuid.UUID, db: Session = Depends(get_db)):
    game_type = db.get(GameType, game_type_id)
    if game_type is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Game type {game_type_id} does not exist")
    return _game_type_response(game_type)


@router.get("/{game_type_name}/versions/{version}")
def get_game_type_endpoint(game_type_name: str, version: int, db: Session = Depends(get_db)):
    return _game_type_response(_game_type_for_request(db, game_type_name, version))


@router.get("/{game_type_name}/versions/{version}/deprecated/{deprecated_at}")
def get_deprecated_game_type_endpoint(game_type_name: str, version: int, deprecated_at: str, db: Session = Depends(get_db)):
    return _game_type_response(_deprecated_game_type_for_request(db, game_type_name, version, deprecated_at))


@router.post("", status_code=status.HTTP_201_CREATED)
def create_game_type_endpoint(payload: GameTypeCreateRequest, db: Session = Depends(get_db)):
    existing = db.query(GameType).filter_by(name=payload.name, version=payload.version, deprecated_at=None).one_or_none()
    if existing is not None:
        return _conflict(
            "game_type_exists",
            f"Game type '{payload.name}' v{payload.version} already exists; choose a different name or version and resubmit.",
            [_describe(db, existing, reason="an active game type with this name and version already exists")],
            [payload.name],
        )

    parent = None
    if payload.derived_from is not None:
        parent = _game_type_for_request(db, payload.derived_from.name, payload.derived_from.version)
        parent_ancestors = _ancestors(parent)
        if len(parent_ancestors) + 1 > MAX_DERIVATION_DEPTH:
            return _conflict(
                "derivation_depth_exceeded",
                (
                    f"Game types can currently only be derived {MAX_DERIVATION_DEPTH} level(s) deep, and {_game_type_label(parent)} "
                    f"is itself derived; derive from a game type that is not derived instead."
                ),
                [
                    _describe(db, parent, reason="the requested parent is itself a derived game type"),
                    *(_describe(db, ancestor, reason=f"ancestor of {_game_type_label(parent)}") for ancestor in parent_ancestors),
                ],
                [payload.name, parent.name],
            )

    game_type = GameType(
        name=payload.name,
        version=payload.version,
        is_generic=payload.is_generic,
        derived_from_id=parent.id if parent is not None else None,
    )
    db.add(game_type)
    db.commit()
    db.refresh(game_type)
    return _game_type_response(game_type)


@router.post("/{game_type_name}/versions/{version}/deprecate")
def deprecate_game_type_endpoint(game_type_name: str, version: int, db: Session = Depends(get_db)):
    """Deprecate a game type, keeping its config, and prompt every derived game type to rebase through ancestor-change review."""
    game_type = _game_type_for_request(db, game_type_name, version)
    queued = _deprecate_game_type(db, game_type)
    db.commit()
    return {"status": "deprecated", "game_type": _game_type_response(game_type), "queued_changes": queued}


@router.delete("/{game_type_name}/versions/{version}")
def delete_game_type_endpoint(game_type_name: str, version: int, db: Session = Depends(get_db)):
    """Delete a game type once all of its config has been deleted.

    Config under the game type blocks the delete (409). If history records or derived game types still reference it,
    it is deprecated instead of deleted, derived game types are prompted to rebase, and the response carries a warning
    listing what prevented the delete.
    """
    game_type = _game_type_for_request(db, game_type_name, version)
    references = _referencing_rows(db, GameType.__table__, game_type.id)
    derived = [row for row in references if row["via"] == _DERIVED_FROM_REFERENCE]
    history = [row for row in references if row["kind"] in _HISTORY_TABLES]
    config = [row for row in references if row not in derived and row not in history]

    if config:
        references_by_column: dict[str, int] = {}
        for row in config:
            references_by_column[row["via"]] = references_by_column.get(row["via"], 0) + 1
        return _conflict(
            "game_type_has_config",
            f"Game type '{game_type_name}' v{version} still has {len(config)} config resource(s); delete them first, then resubmit.",
            [{**row, "reason": f"config belonging to game type '{game_type_name}' v{version} (via {row['via']})"} for row in config],
            [game_type_name],
            details={"references": references_by_column},
        )

    if derived or history:
        queued = _deprecate_game_type(db, game_type)
        db.commit()
        warning = WarningResponse(
            warning_type="deprecated_instead_of_deleted",
            message=(
                f"Game type '{game_type_name}' v{version} is still referenced by {len(history)} history record(s) and "
                f"{len(derived)} derived game type(s), so it was deprecated instead of deleted."
            ),
            affected_game_types=[game_type_name],
            blocking_resources=[
                *({**row, "reason": "history record referencing this game type"} for row in history),
                *({**row, "reason": "derived game type; prompted to rebase"} for row in derived),
            ],
        )
        return {
            "status": "deprecated",
            "game_type": _game_type_response(game_type),
            "queued_changes": queued,
            "warning": warning.model_dump(mode="json"),
        }

    deleted = _game_type_response(game_type)
    db.delete(game_type)
    db.commit()
    return {"status": "deleted", "deleted": deleted}


@router.delete("/{game_type_name}/versions/{version}/deprecated/{deprecated_at}")
def delete_deprecated_game_type_endpoint(game_type_name: str, version: int, deprecated_at: str, db: Session = Depends(get_db)):
    """Permanently delete a deprecated game type once nothing references it any more."""
    game_type = _deprecated_game_type_for_request(db, game_type_name, version, deprecated_at)
    references = _referencing_rows(db, GameType.__table__, game_type.id)
    if references:
        return _conflict(
            "game_type_in_use",
            f"Deprecated game type '{game_type_name}' v{version} is still referenced by {len(references)} resource(s); remove them first, then resubmit.",
            [{**row, "reason": f"references this game type through {row['via']}"} for row in references],
            [game_type_name],
        )

    deleted = _game_type_response(game_type)
    db.delete(game_type)
    db.commit()
    return {"status": "deleted", "deleted": deleted}


def _game_type_label(game_type: GameType) -> str:
    return f"'{game_type.name}' v{game_type.version}"


def _copy_source_row(db: Session, model: type, source_game_type: GameType, name: str, label: str):
    row = db.query(model).filter_by(name=name, game_type_id=source_game_type.id).one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{label} '{name}' does not exist in source game type {_game_type_label(source_game_type)}",
        )
    return row


def _copy_named_row(db: Session, model: type, source_game_type: GameType, target_game_type: GameType, name: str, label: str):
    _copy_source_row(db, model, source_game_type, name, label)
    row = db.query(model).filter_by(name=name, game_type_id=target_game_type.id).one_or_none()
    if row is None:
        row = model(name=name, game_type_id=target_game_type.id)
        db.add(row)
        db.flush()
    return row


class _CopyDependencies:
    """Resolve what copied resources depend on in the target game type, recording every one that is missing.

    Collecting instead of failing on the first gap lets the conflict response list every resource the
    request still needs, so it can be fixed in a single resubmission.
    """

    def __init__(self, db: Session, target_game_type: GameType):
        self.db = db
        self.target_game_type = target_game_type
        self.missing: dict[tuple[str, str], dict[str, Any]] = {}

    def require(self, model: type, name: str, label: str, required_by: dict[str, str]):
        row = self.db.query(model).filter_by(name=name, game_type_id=self.target_game_type.id).one_or_none()
        if row is None:
            entry = self.missing.setdefault(
                (model.__tablename__, name),
                {
                    "kind": model.__tablename__,
                    "name": name,
                    "game_type": {"id": str(self.target_game_type.id), "name": self.target_game_type.name, "version": self.target_game_type.version},
                    "reason": f"{label} '{name}' does not exist in the target game type; add it to the copy request or create it first",
                    "required_by": [],
                },
            )
            if required_by not in entry["required_by"]:
                entry["required_by"].append(required_by)
        return row

    def entity_target(self, source_row: Any, required_by: dict[str, str]) -> dict[str, Any] | None:
        """Map a source row's entity type or entity class binding onto the same-named target resource, or None if it is missing."""
        if source_row.entity_class_id is not None:
            entity_class = self.require(EntityClass, source_row.entity_class.name, "entity class", required_by)
            return None if entity_class is None else {"entity_type_id": None, "entity_class_id": entity_class.id}
        if source_row.entity_type_id is not None:
            entity_type = self.require(EntityType, source_row.entity_type.name, "entity type", required_by)
            return None if entity_type is None else {"entity_type_id": entity_type.id, "entity_class_id": None}
        return {"entity_type_id": None, "entity_class_id": None}


@router.post("/{game_type_name}/versions/{version}/config/copy")
def copy_game_type_config_endpoint(game_type_name: str, version: int, payload: BulkCopyRequest, db: Session = Depends(get_db)):
    """Copy explicitly listed resources from a source game type into this one.

    Every listed resource must exist in the source game type. Anything a copied resource depends on
    (for example a relationship role's participant type and entity type or class) must already exist in
    the target game type or be listed in the same request; nothing is created implicitly. If anything is
    missing, nothing is copied and the 409 response lists every missing resource and what requires it.
    Relationship roles, effect configs and resolver config parent links are copied from the source game type.
    """
    target_game_type = _game_type_for_request(db, game_type_name, version)
    source_name = payload.source_game_type.name
    source_game_type = _game_type_for_request(db, source_name, payload.source_game_type.version)
    if target_game_type.id == source_game_type.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Source and target game type must be different")

    dependencies = _CopyDependencies(db, target_game_type)
    copied: dict[str, list[str]] = {
        "entity_types": [],
        "entity_classes": [],
        "entity_type_classes": [],
        "participant_types": [],
        "relationship_types": [],
        "action_types": [],
        "action_resolver_configs": [],
    }

    for name in _copy_target_game_type_resource_names(payload.entity_types):
        _copy_named_row(db, EntityType, source_game_type, target_game_type, name, "entity type")
        copied["entity_types"].append(name)

    for name in _copy_target_game_type_resource_names(payload.entity_classes):
        _copy_named_row(db, EntityClass, source_game_type, target_game_type, name, "entity class")
        copied["entity_classes"].append(name)

    for item in payload.entity_type_classes or []:
        entity_type_name = item.get("entity_type")
        entity_class_name = item.get("entity_class")
        key = f"{entity_type_name}:{entity_class_name}"
        source_link = (
            db.query(EntityTypeClass)
            .join(EntityType, EntityType.id == EntityTypeClass.entity_type_id)
            .join(EntityClass, EntityClass.id == EntityTypeClass.entity_class_id)
            .filter(
                EntityTypeClass.game_type_id == source_game_type.id,
                EntityType.name == entity_type_name,
                EntityClass.name == entity_class_name,
            )
            .one_or_none()
        )
        if source_link is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"entity type class membership '{key}' does not exist in source game type {_game_type_label(source_game_type)}",
            )
        required_by = {"kind": EntityTypeClass.__tablename__, "name": key}
        entity_type = dependencies.require(EntityType, entity_type_name, "entity type", required_by)
        entity_class = dependencies.require(EntityClass, entity_class_name, "entity class", required_by)
        if entity_type is None or entity_class is None:
            continue
        link = db.query(EntityTypeClass).filter_by(game_type_id=target_game_type.id, entity_type_id=entity_type.id, entity_class_id=entity_class.id).one_or_none()
        if link is None:
            db.add(EntityTypeClass(game_type_id=target_game_type.id, entity_type_id=entity_type.id, entity_class_id=entity_class.id))
            db.flush()
        copied["entity_type_classes"].append(key)

    for name in _copy_target_game_type_resource_names(payload.participant_types):
        _copy_named_row(db, ParticipantType, source_game_type, target_game_type, name, "participant type")
        copied["participant_types"].append(name)

    for name in _copy_target_game_type_resource_names(payload.action_types):
        _copy_named_row(db, ActionType, source_game_type, target_game_type, name, "action type")
        copied["action_types"].append(name)

    for name in _copy_target_game_type_resource_names(payload.relationship_types):
        source_relationship_type = _copy_source_row(db, RelationshipType, source_game_type, name, "relationship type")
        relationship_type = _copy_named_row(db, RelationshipType, source_game_type, target_game_type, name, "relationship type")
        required_by = {"kind": RelationshipType.__tablename__, "name": name}
        for source_role in source_relationship_type.participant_types:
            participant_type = dependencies.require(ParticipantType, source_role.participant_type.name, "participant type", required_by)
            target = dependencies.entity_target(source_role, required_by)
            if participant_type is None or target is None:
                continue
            existing_role = (
                db.query(RelationshipTypeParticipantType)
                .filter_by(relationship_type_id=relationship_type.id, participant_type_id=participant_type.id, **target)
                .one_or_none()
            )
            if existing_role is None:
                db.add(RelationshipTypeParticipantType(relationship_type_id=relationship_type.id, participant_type_id=participant_type.id, **target))
                db.flush()
        copied["relationship_types"].append(name)

    requested_config_names = _copy_target_game_type_resource_names(payload.action_resolver_configs)
    config_pairs: list[tuple[ActionResolverConfig, ActionResolverConfig]] = []
    for name in requested_config_names:
        source_config = _copy_source_row(db, ActionResolverConfig, source_game_type, name, "action resolver config")
        required_by = {"kind": ActionResolverConfig.__tablename__, "name": name}
        action_type = None
        if source_config.action_type is not None:
            action_type = dependencies.require(ActionType, source_config.action_type.name, "action type", required_by)
            if action_type is None:
                continue

        target_config = db.query(ActionResolverConfig).filter_by(name=name, game_type_id=target_game_type.id).one_or_none()
        if target_config is None:
            target_config = ActionResolverConfig(
                name=name,
                resolver_key=source_config.resolver_key,
                game_type_id=target_game_type.id,
                action_type_id=action_type.id if action_type else None,
                relationship_type=source_config.relationship_type,
            )
            db.add(target_config)
            db.flush()
        config_pairs.append((source_config, target_config))

        effect = source_config.effect_config
        if effect is not None and target_config.effect_config is None:
            if isinstance(effect, EffectCalculationConfig):
                target_effect = EffectCalculationConfig(
                    action_resolver_config_id=target_config.id,
                    dice_count=effect.dice_count,
                    dice_sides=effect.dice_sides,
                    bonus=effect.bonus,
                    multiplier=effect.multiplier,
                )
                db.add(target_effect)
                db.flush()
                for source_participant in effect.participants:
                    participant_type = dependencies.require(ParticipantType, source_participant.participant_type.name, "participant type", required_by)
                    target = dependencies.entity_target(source_participant, required_by)
                    if participant_type is None or target is None:
                        continue
                    db.add(
                        EffectCalculationParticipant(
                            effect_calculation_config_id=target_effect.id,
                            game_type_id=target_game_type.id,
                            participant_type_id=participant_type.id,
                            entity_binding_type=source_participant.entity_binding_type,
                            **target,
                        )
                    )
            elif isinstance(effect, EntityMutationEffectConfig):
                target = dependencies.entity_target(effect, required_by)
                if target is not None:
                    db.add(
                        EntityMutationEffectConfig(
                            action_resolver_config_id=target_config.id,
                            mutation_kind=effect.mutation_kind,
                            count_dice_count=effect.count_dice_count,
                            count_dice_sides=effect.count_dice_sides,
                            count_bonus=effect.count_bonus,
                            **target,
                        )
                    )
            elif isinstance(effect, RelationshipEffectConfig):
                relationship_type = dependencies.require(RelationshipType, effect.relationship_type.name, "relationship type", required_by)
                if relationship_type is not None:
                    db.add(RelationshipEffectConfig(action_resolver_config_id=target_config.id, relationship_type_id=relationship_type.id))
            db.flush()
        copied["action_resolver_configs"].append(name)

    # Parent links are resolved after every listed config exists, so a request may list a tree in any order.
    # A listed parent that could not be created is not reported again; its own missing dependencies already are.
    for source_config, target_config in config_pairs:
        if source_config.parent is None:
            continue
        parent_name = source_config.parent.name
        parent = db.query(ActionResolverConfig).filter_by(name=parent_name, game_type_id=target_game_type.id).one_or_none()
        if parent is None:
            if parent_name not in requested_config_names:
                dependencies.require(ActionResolverConfig, parent_name, "parent action resolver config", {"kind": ActionResolverConfig.__tablename__, "name": source_config.name})
            continue
        target_config.parent_id = parent.id

    if dependencies.missing:
        db.rollback()
        return _conflict(
            "missing_copy_dependencies",
            (
                f"{len(dependencies.missing)} resource(s) required by this copy do not exist in target game type "
                f"{_game_type_label(target_game_type)}; nothing was copied. Add them to the request and resubmit."
            ),
            list(dependencies.missing.values()),
            [target_game_type.name],
            details={
                "source_game_type": {"name": source_game_type.name, "version": source_game_type.version},
                "target_game_type": {"name": target_game_type.name, "version": target_game_type.version},
            },
        )

    db.commit()
    return {"status": "ok", "source_game_type": source_name, "target_game_type": game_type_name, "copied": copied}


@router.post("/{game_type_name}/versions/{version}/config/export")
def export_game_type_config_endpoint(game_type_name: str, version: int, db: Session = Depends(get_db)):
    _game_type_for_request(db, game_type_name, version)
    return export_game_type_config(db, game_type_name, version)


@router.post("/{game_type_name}/versions/{version}/config/review/{change_id}/accept")
def accept_game_type_change(game_type_name: str, version: int, change_id: str, db: Session = Depends(get_db)):
    game_type = _game_type_for_request(db, game_type_name, version)
    if game_type.derived_from_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This game type does not have a parent review lineage.")

    change = db.query(GameTypeResourceChange).filter_by(id=uuid.UUID(change_id)).one_or_none()
    if change is None or change.derived_game_type_id != game_type.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review change not found for this game type.")

    # TODO: an accepted ancestor-deprecated change (resource_kind "game_type") should trigger a rebase onto a new
    # ancestor; see _deprecate_game_type. For now acceptance only records the acknowledgement.
    change.status = "accepted"
    db.commit()
    return {"status": "ok", "updated_status": change.status, "change_id": str(change.id), "game_type": game_type_name}


@router.post("/{game_type_name}/versions/{version}/config/review/{change_id}/reject")
def reject_game_type_change(game_type_name: str, version: int, change_id: str, db: Session = Depends(get_db)):
    game_type = _game_type_for_request(db, game_type_name, version)
    if game_type.derived_from_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This game type does not have a parent review lineage.")

    change = db.query(GameTypeResourceChange).filter_by(id=uuid.UUID(change_id)).one_or_none()
    if change is None or change.derived_game_type_id != game_type.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review change not found for this game type.")

    change.status = "rejected"
    db.commit()
    return {"status": "ok", "updated_status": change.status, "change_id": str(change.id), "game_type": game_type_name}


@router.post("/{game_type_name}/versions/{version}/config/import/preview")
def import_game_type_preview(game_type_name: str, version: int, payload: BulkConfigImportPreviewRequest, db: Session = Depends(get_db)):
    game_type = _game_type_for_request(db, game_type_name, version)
    if game_type.derived_from_id is not None:
        return {"status": "pending_review", "message": f"Import into derived game type '{game_type_name}' will be queued for review before it becomes active.", "changes": []}

    payload_dict = payload.model_dump(exclude_none=True)
    conflict = _import_conflict(db, game_type, payload_dict)
    if conflict is not None:
        return conflict
    return {"status": "ok", "message": "No conflicts detected; import can proceed."}


@router.post("/{game_type_name}/versions/{version}/config/import")
def import_game_type_config_endpoint(game_type_name: str, version: int, payload: BulkConfigImportPreviewRequest, db: Session = Depends(get_db)):
    game_type = _game_type_for_request(db, game_type_name, version)
    payload_dict = payload.model_dump(exclude_none=True)
    if game_type.derived_from_id is not None:
        result = _import_or_400(db, game_type, payload_dict)
        db.commit()
        return result

    conflict = _import_conflict(db, game_type, payload_dict)
    if conflict is not None:
        return conflict
    result = _import_or_400(db, game_type, payload_dict)
    db.commit()
    return result


@router.post("/{game_type_name}/versions/{version}/config/delete/preview")
def delete_game_type_preview(game_type_name: str, version: int, payload: BulkDeletePreviewRequest, db: Session = Depends(get_db)):
    game_type = _game_type_for_request(db, game_type_name, version)
    if game_type.derived_from_id is not None:
        return {"status": "pending_review", "message": f"Delete manifest for derived game type '{game_type_name}' will be queued for review instead of mutating the active ruleset.", "changes": []}

    conflict = _delete_conflict(db, game_type, payload.model_dump(exclude_none=True))
    if conflict is not None:
        return conflict
    return {"status": "ok", "message": "No cascade risk detected for this explicit delete manifest."}


@router.post("/{game_type_name}/versions/{version}/config/delete")
def delete_game_type_config_endpoint(game_type_name: str, version: int, payload: BulkDeletePreviewRequest, db: Session = Depends(get_db)):
    game_type = _game_type_for_request(db, game_type_name, version)
    if game_type.derived_from_id is not None:
        source_game_type = db.get(GameType, game_type.derived_from_id)
        if source_game_type is None:
            return _conflict(
                "missing_parent_game_type",
                f"Game type '{game_type_name}' v{version} is derived from a game type that does not exist; nothing was queued.",
                [
                    _describe(
                        db,
                        game_type,
                        reason="derived_from_id references a game type that does not exist",
                        missing_parent_id=str(game_type.derived_from_id),
                    )
                ],
                [game_type_name],
            )
        for item in payload.model_dump(exclude_none=True).get("relationship_types", []) or []:
            name = item.get("name")
            if not name:
                continue
            row = db.query(RelationshipType).filter_by(name=name, game_type_id=source_game_type.id).one_or_none()
            if row is None:
                continue
            db.add(
                GameTypeResourceChange(
                    derived_game_type_id=game_type.id,
                    source_game_type_id=source_game_type.id,
                    resource_kind="relationship_type",
                    resource_key=name,
                    source_resource_id=row.id,
                    status="pending",
                    snapshot_payload='{"action": "delete", "name": "%s"}' % name,
                )
            )
        for item in payload.model_dump(exclude_none=True).get("action_resolver_configs", []) or []:
            name = item.get("name")
            if not name:
                continue
            row = db.query(ActionResolverConfig).filter_by(name=name, game_type_id=source_game_type.id).one_or_none()
            if row is None:
                continue
            db.add(
                GameTypeResourceChange(
                    derived_game_type_id=game_type.id,
                    source_game_type_id=source_game_type.id,
                    resource_kind="action_resolver_config",
                    resource_key=name,
                    source_resource_id=row.id,
                    status="pending",
                    snapshot_payload='{"action": "delete", "name": "%s"}' % name,
                )
            )
        db.commit()
        return {"status": "pending_review", "message": "Derived delete requests were queued for review.", "game_type": game_type_name}

    conflict = _delete_conflict(db, game_type, payload.model_dump(exclude_none=True))
    if conflict is not None:
        return conflict

    deleted = []
    for item in payload.model_dump(exclude_none=True).get("relationship_types", []) or []:
        name = item.get("name")
        if not name:
            continue
        row = db.query(RelationshipType).filter_by(name=name, game_type_id=game_type.id).one_or_none()
        if row is None:
            continue
        db.delete(row)
        deleted.append({"type": "relationship_type", "name": name})

    for item in payload.model_dump(exclude_none=True).get("action_resolver_configs", []) or []:
        name = item.get("name")
        if not name:
            continue
        row = db.query(ActionResolverConfig).filter_by(name=name, game_type_id=game_type.id).one_or_none()
        if row is None:
            continue
        db.delete(row)
        deleted.append({"type": "action_resolver_config", "name": name})

    db.commit()
    return {"status": "ok", "deleted": deleted, "game_type": game_type_name}
