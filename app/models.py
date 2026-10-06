import inspect
import uuid

from sqlalchemy import JSON, Boolean, CheckConstraint, Column, DateTime, Index, Integer, String, Text, ForeignKey, UniqueConstraint, event, not_, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Session, relationship

from app.database import Base, SessionLocal


class RoleRequirement:
    """Marker for participant-role requirements that can be attached to resolver method parameters."""

    def __init__(self, role_name: str, entity_type=None):
        self.role_name = role_name
        self.entity_type = entity_type


class ParticipantMap(dict):
    """Typed helper for resolver participant pools keyed by role name."""

    pass


_ACTION_RESOLVER_REGISTRY = {}


def _register_action_resolver_class(resolver_cls):
    if not isinstance(resolver_cls, type) or not issubclass(resolver_cls, BaseActionResolver):
        raise TypeError("Action resolvers must be classes that subclass BaseActionResolver.")

    if getattr(resolver_cls, "__resolver_registered__", False):
        return resolver_cls

    resolver_name = getattr(resolver_cls, "__resolver_name__", None) or getattr(resolver_cls, "__resolver_key__", None) or resolver_cls.__name__
    resolver_cls.__resolver_name__ = resolver_name
    resolver_cls.__resolver_key__ = resolver_name
    resolver_cls.__resolver_registered__ = True
    _ACTION_RESOLVER_REGISTRY[resolver_name] = resolver_cls
    return resolver_cls


class BaseActionResolver:
    """Base implementation for an action resolver.

    Resolver subclasses are callable and receive a config payload and a dictionary of
    participant groups keyed by participant role name. The resolver contract is kept in
    the method signatures so the runtime can introspect the expected config and inputs
    without duplicating metadata in a separate decorator.
    """

    __resolver_key__ = None
    __resolver_name__ = None
    __resolver_registered__ = False

    def __init__(self, config: "ActionEffectConfig", participants: dict[str, list["Entity"]]):
        if not isinstance(participants, dict):
            raise TypeError("Action resolvers expect a dictionary of participant lists keyed by participant type name.")
        self.config = config
        self.participants = {key: list(value) for key, value in participants.items()}

    def __call__(self):
        raise NotImplementedError("Resolver implementations must define __call__().")

    @classmethod
    def contract(cls):
        init_signature = inspect.signature(cls.__init__)
        call_signature = inspect.signature(cls.__call__)
        return {
            "config_type": init_signature.parameters.get("config").annotation,
            "participants_type": init_signature.parameters.get("participants").annotation,
            "return_type": call_signature.return_annotation,
        }

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        _register_action_resolver_class(cls)


# A game type is active while deprecated_at is null. Deprecating it keeps the row (and its config) so history records and
# derived game types can still reference it, and frees its name and version for a new active game type. Deprecated game
# types stay uniquely addressable by (name, version, deprecated_at). See docs/game_type_lifecycle.md.
class GameType(Base):
    __tablename__ = "game_type"
    __table_args__ = (
        # Two partial indexes because a single unique constraint including the nullable deprecated_at would treat every
        # active row (deprecated_at NULL) as distinct, allowing duplicate active game types.
        Index(
            "uq_game_type_active_name_version",
            "name",
            "version",
            unique=True,
            sqlite_where=text("deprecated_at IS NULL"),
            postgresql_where=text("deprecated_at IS NULL"),
        ),
        Index(
            "uq_game_type_deprecated_name_version",
            "name",
            "version",
            "deprecated_at",
            unique=True,
            sqlite_where=text("deprecated_at IS NOT NULL"),
            postgresql_where=text("deprecated_at IS NOT NULL"),
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    name = Column(String(255), nullable=False)
    version = Column(Integer, nullable=False, default=1)
    derived_from_id = Column(UUID(as_uuid=True), ForeignKey("game_type.id"), nullable=True)
    # generic game types are used for creating entity type collections, ex "plants" or "weapons" for game makers to draw from when creating new systems.
    is_generic = Column(Boolean, nullable=False, default=False)
    # UTC, stored naive; null while the game type is active.
    deprecated_at = Column(DateTime, nullable=True, default=None)

    derived_from = relationship("GameType", remote_side="GameType.id", backref="derived_versions")

    @property
    def is_deprecated(self) -> bool:
        return self.deprecated_at is not None

    @property
    def signature(self) -> dict:
        """The name and version that address this game type in API requests."""
        return {"name": self.name, "version": self.version}


class GameTypeResourceChange(Base):
    __tablename__ = "game_type_resource_change"
    __table_args__ = (
        UniqueConstraint(
            "derived_game_type_id",
            "source_game_type_id",
            "resource_kind",
            "resource_key",
            name="uq_game_type_resource_change",
        ),
        CheckConstraint(
            "status IN ('pending', 'accepted', 'rejected', 'superseded')",
            name="ck_game_type_resource_change_status",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    derived_game_type_id = Column(UUID(as_uuid=True), ForeignKey("game_type.id"), nullable=False)
    source_game_type_id = Column(UUID(as_uuid=True), ForeignKey("game_type.id"), nullable=False)
    resource_kind = Column(String(64), nullable=False)
    resource_key = Column(String(255), nullable=False)
    source_resource_id = Column(UUID(as_uuid=True), nullable=True)
    derived_resource_id = Column(UUID(as_uuid=True), nullable=True)
    status = Column(String(32), nullable=False, default="pending")
    snapshot_payload = Column(Text, nullable=False)
    review_note = Column(String(1024), nullable=True)
    reviewed_by = Column(String(255), nullable=True)
    reviewed_at = Column(String(64), nullable=True)

    derived_game_type = relationship(
        "GameType",
        foreign_keys=[derived_game_type_id],
        backref="resource_changes_received",
    )
    source_game_type = relationship(
        "GameType",
        foreign_keys=[source_game_type_id],
        backref="resource_changes_emitted",
    )


# entity_type is ex "long sword", while entity_class is ex "weapon".  Class membership is optional for types, and a type can belong to multiple classes.
class EntityType(Base):
    __tablename__ = "entity_type"
    __table_args__ = (UniqueConstraint("game_type_id", "name", name="uq_entity_type_game_type_name"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    game_type_id = Column(UUID(as_uuid=True), ForeignKey("game_type.id"), nullable=False)
    name = Column(String(255), nullable=False)
    entity_type_classes = relationship("EntityTypeClass", back_populates="entity_type")
    entities = relationship("Entity", back_populates="entity_type")


class Entity(Base):
    __tablename__ = "entity"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    name = Column(String(255), nullable=False)
    game_type_id = Column(UUID(as_uuid=True), ForeignKey("game_type.id"), nullable=False)
    entity_type_id = Column(UUID(as_uuid=True), ForeignKey("entity_type.id"), nullable=False)
    parent_id = Column(UUID(as_uuid=True), ForeignKey("entity.id"), nullable=True)

    game_type = relationship("GameType")
    entity_type = relationship("EntityType", back_populates="entities")
    parent = relationship("Entity", remote_side="Entity.id", backref="children")




class EntityClass(Base):
    __tablename__ = "entity_class"
    __table_args__ = (UniqueConstraint("game_type_id", "name", name="uq_entity_class_game_type_name"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    game_type_id = Column(UUID(as_uuid=True), ForeignKey("game_type.id"), nullable=False)
    name = Column(String(255), nullable=False)


# enttity_type_class can pull double duty by also acting as a way to create generic entity type collections for game makers to pull from
# when creating new game systems.  This just means there need to be genric game types to link to to use as categories as well
class EntityTypeClass(Base):
    __tablename__ = "entity_type_class"
    __table_args__ = (
        UniqueConstraint("game_type_id", "entity_type_id", "entity_class_id", name="uq_entity_type_class_game"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    game_type_id = Column(UUID(as_uuid=True), ForeignKey("game_type.id"), nullable=False)
    entity_type_id = Column(UUID(as_uuid=True), ForeignKey("entity_type.id"), nullable=False)
    entity_class_id = Column(UUID(as_uuid=True), ForeignKey("entity_class.id"), nullable=False)

    game_type = relationship("GameType")
    entity_type = relationship("EntityType", back_populates="entity_type_classes")
    entity_class = relationship("EntityClass")


# action_type is the classification of effects, ex "fireball".  calculation_config is the effect for a particular interaction for an action_type,
# for example, fire damage to a tree, or blunt force to a door
class ActionType(Base):
    __tablename__ = "action_type"
    __table_args__ = (UniqueConstraint("game_type_id", "name", name="uq_action_type_game_type_name"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    game_type_id = Column(UUID(as_uuid=True), ForeignKey("game_type.id"), nullable=False)
    name = Column(String(255), nullable=False)


class ParticipantType(Base):
    __tablename__ = "participant_type"
    __table_args__ = (UniqueConstraint("game_type_id", "name", name="uq_participant_type_game_type_name"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    name = Column(String(255), nullable=False)
    game_type_id = Column(UUID(as_uuid=True), ForeignKey("game_type.id"), nullable=False)

    game_type = relationship("GameType")


class ActionResolverConfig(Base):
    __tablename__ = "action_resolver_config"
    __table_args__ = (UniqueConstraint("game_type_id", "name", name="uq_action_resolver_config_game_type_name"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    name = Column(String(255), nullable=False)
    resolver_key = Column(String(255), nullable=False)
    game_type_id = Column(UUID(as_uuid=True), ForeignKey("game_type.id"), nullable=False)
    action_type_id = Column(UUID(as_uuid=True), ForeignKey("action_type.id"), nullable=True)
    parent_id = Column(UUID(as_uuid=True), ForeignKey("action_resolver_config.id"), nullable=True)
    relationship_type = Column(String(32), nullable=False, default="child")

    game_type = relationship("GameType")
    action_type = relationship("ActionType")
    parent = relationship("ActionResolverConfig", remote_side="ActionResolverConfig.id", back_populates="children")
    children = relationship("ActionResolverConfig", back_populates="parent", cascade="all, delete-orphan")
    effect_config = relationship(
        "ActionEffectConfig",
        back_populates="action_resolver_config",
        uselist=False,
        foreign_keys="[ActionEffectConfig.action_resolver_config_id]",
    )


class ActionEffectConfig(Base):
    __tablename__ = "action_effect_config"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    action_resolver_config_id = Column(UUID(as_uuid=True), ForeignKey("action_resolver_config.id"), nullable=False, unique=True)
    effect_kind = Column(String(64), nullable=False)

    action_resolver_config = relationship(
        "ActionResolverConfig",
        back_populates="effect_config",
        foreign_keys="[ActionEffectConfig.action_resolver_config_id]",
    )

    __mapper_args__ = {
        "polymorphic_on": "effect_kind",
        "polymorphic_identity": "base",
    }


class EffectCalculationConfig(ActionEffectConfig):
    __tablename__ = "effect_calculation_config"

    id = Column(UUID(as_uuid=True), ForeignKey("action_effect_config.id"), primary_key=True)
    dice_count = Column(Integer, nullable=False)
    dice_sides = Column(Integer, nullable=False)
    bonus = Column(Integer, nullable=False, default=0)
    multiplier = Column(Integer, nullable=False, default=1)

    __mapper_args__ = {
        "polymorphic_identity": "calculation",
    }

    participants = relationship(
        "EffectCalculationParticipant",
        back_populates="effect_calculation_config",
        cascade="all, delete-orphan",
    )


class EntityMutationEffectConfig(ActionEffectConfig):
    __tablename__ = "entity_mutation_effect_config"

    id = Column(UUID(as_uuid=True), ForeignKey("action_effect_config.id"), primary_key=True)
    mutation_kind = Column(String(64), nullable=False)
    entity_type_id = Column(UUID(as_uuid=True), ForeignKey("entity_type.id"), nullable=True)
    entity_class_id = Column(UUID(as_uuid=True), ForeignKey("entity_class.id"), nullable=True)
    count_dice_count = Column(Integer, nullable=True)
    count_dice_sides = Column(Integer, nullable=True)
    count_bonus = Column(Integer, nullable=True, default=0)

    entity_type = relationship("EntityType")
    entity_class = relationship("EntityClass")

    __mapper_args__ = {
        "polymorphic_identity": "entity_mutation",
    }


class RelationshipEffectConfig(ActionEffectConfig):
    __tablename__ = "relationship_effect_config"

    id = Column(UUID(as_uuid=True), ForeignKey("action_effect_config.id"), primary_key=True)
    relationship_type_id = Column(UUID(as_uuid=True), ForeignKey("relationship_type.id"), nullable=False)
    relationship_type = relationship("RelationshipType")

    __mapper_args__ = {
        "polymorphic_identity": "relationship",
    }


class EffectCalculationParticipant(Base):
    __tablename__ = "effect_calculation_participant"
    __table_args__ = (
        UniqueConstraint(
            "effect_calculation_config_id",
            "game_type_id",
            "participant_type_id",
            "entity_binding_type",
            "entity_type_id",
            "entity_class_id",
            name="uq_effect_config_participant_game_role",
        ),
        CheckConstraint(
            "((entity_type_id IS NOT NULL) <> (entity_class_id IS NOT NULL))",
            name="ck_effect_config_participant_exactly_one_type",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    effect_calculation_config_id = Column(UUID(as_uuid=True), ForeignKey("effect_calculation_config.id"), nullable=False)
    game_type_id = Column(UUID(as_uuid=True), ForeignKey("game_type.id"), nullable=False)
    participant_type_id = Column(UUID(as_uuid=True), ForeignKey("participant_type.id"), nullable=False)
    entity_binding_type = Column(String(32), nullable=False)  # entity_type or entity_class
    entity_type_id = Column(UUID(as_uuid=True), ForeignKey("entity_type.id"), nullable=True)
    entity_class_id = Column(UUID(as_uuid=True), ForeignKey("entity_class.id"), nullable=True)

    effect_calculation_config = relationship(
        "EffectCalculationConfig",
        foreign_keys=[effect_calculation_config_id],
        back_populates="participants",
    )
    game_type = relationship("GameType")
    participant_type = relationship("ParticipantType")
    entity_type = relationship("EntityType")
    entity_class = relationship("EntityClass")


class RelationshipType(Base):
    __tablename__ = "relationship_type"
    __table_args__ = (UniqueConstraint("game_type_id", "name", name="uq_relationship_type_game_type_name"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    name = Column(String(255), nullable=False)
    game_type_id = Column(UUID(as_uuid=True), ForeignKey("game_type.id"), nullable=False)

    game_type = relationship("GameType")
    participant_types = relationship(
        "RelationshipTypeParticipantType",
        back_populates="relationship_type",
        cascade="all, delete-orphan",
    )
    relationships = relationship("Relationship", back_populates="relationship_type")


# A participant role binds to either an entity type or an entity class. A class binding admits every entity type that
# belongs to the class, much like a permission granted to a user group applies to each of its members.
class RelationshipTypeParticipantType(Base):
    __tablename__ = "relationship_type_participant_type"
    __table_args__ = (
        CheckConstraint(
            "((entity_type_id IS NOT NULL) <> (entity_class_id IS NOT NULL))",
            name="ck_relationship_type_participant_type_exactly_one_target",
        ),
        # Separate partial indexes because a unique constraint spanning both nullable columns would treat NULLs as distinct.
        Index(
            "uq_relationship_type_participant_entity_type",
            "relationship_type_id",
            "participant_type_id",
            "entity_type_id",
            unique=True,
            sqlite_where=text("entity_type_id IS NOT NULL"),
            postgresql_where=text("entity_type_id IS NOT NULL"),
        ),
        Index(
            "uq_relationship_type_participant_entity_class",
            "relationship_type_id",
            "participant_type_id",
            "entity_class_id",
            unique=True,
            sqlite_where=text("entity_class_id IS NOT NULL"),
            postgresql_where=text("entity_class_id IS NOT NULL"),
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    relationship_type_id = Column(UUID(as_uuid=True), ForeignKey("relationship_type.id"), nullable=False)
    participant_type_id = Column(UUID(as_uuid=True), ForeignKey("participant_type.id"), nullable=False)
    entity_type_id = Column(UUID(as_uuid=True), ForeignKey("entity_type.id"), nullable=True)
    entity_class_id = Column(UUID(as_uuid=True), ForeignKey("entity_class.id"), nullable=True)

    relationship_type = relationship("RelationshipType", back_populates="participant_types")
    participant_type = relationship("ParticipantType")
    entity_type = relationship("EntityType")
    entity_class = relationship("EntityClass")


class Relationship(Base):
    __tablename__ = "relationship"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    name = Column(String(255), nullable=False)
    relationship_type_id = Column(UUID(as_uuid=True), ForeignKey("relationship_type.id"), nullable=False)
    game_type_id = Column(UUID(as_uuid=True), ForeignKey("game_type.id"), nullable=False)

    relationship_type = relationship("RelationshipType", back_populates="relationships")
    game_type = relationship("GameType")
    participants = relationship("RelationshipParticipant", back_populates="relationship_", cascade="all, delete-orphan")


class RelationshipParticipant(Base):
    __tablename__ = "relationship_participant"
    __table_args__ = (
        UniqueConstraint(
            "relationship_id",
            "participant_type_id",
            name="uq_relationship_participant_role",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    relationship_id = Column(UUID(as_uuid=True), ForeignKey("relationship.id"), nullable=False)
    participant_type_id = Column(UUID(as_uuid=True), ForeignKey("participant_type.id"), nullable=False)
    entity_id = Column(UUID(as_uuid=True), ForeignKey("entity.id"), nullable=False)

    relationship_ = relationship("Relationship", back_populates="participants")
    participant_type = relationship("ParticipantType")
    entity = relationship("Entity")


# History records describe what happened when an action resolved. They deliberately hold no foreign keys to config or
# runtime entity rows: those rows can change or be deleted, and the config tables are not versioned. Instead each
# resolution stores a snapshot of the config tree it ran (config_snapshot, in the export format) and each participant a
# snapshot of its entity, with the original ids kept as plain values for tracing. The one link out is game_type_id,
# which keeps a game type with history from being deleted; deleting it deprecates it instead.
class Resolution(Base):
    __tablename__ = "resolution"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    game_type_id = Column(UUID(as_uuid=True), ForeignKey("game_type.id"), nullable=False)
    action_type_id = Column(UUID(as_uuid=True), nullable=True)
    action_type_name = Column(String(255), nullable=True)
    action_resolver_config_id = Column(UUID(as_uuid=True), nullable=False)
    action_resolver_config_name = Column(String(255), nullable=False)
    config_snapshot = Column(JSON, nullable=False)
    snapshot_format_version = Column(Integer, nullable=False)
    status = Column(String(32), nullable=False, default="pending")
    created_at = Column(DateTime, nullable=False)

    game_type = relationship("GameType")
    participants = relationship("ResolutionParticipant", back_populates="resolution", cascade="all, delete-orphan")
    steps = relationship("ResolutionStep", back_populates="resolution", cascade="all, delete-orphan", order_by="ResolutionStep.order")


class ResolutionParticipant(Base):
    __tablename__ = "resolution_participant"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    resolution_id = Column(UUID(as_uuid=True), ForeignKey("resolution.id"), nullable=False)
    role_name = Column(String(255), nullable=False)
    participant_type_id = Column(UUID(as_uuid=True), nullable=True)
    entity_id = Column(UUID(as_uuid=True), nullable=False)
    entity_snapshot = Column(JSON, nullable=False)

    resolution = relationship("Resolution", back_populates="participants")


class ResolutionStep(Base):
    __tablename__ = "resolution_step"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    resolution_id = Column(UUID(as_uuid=True), ForeignKey("resolution.id"), nullable=False)
    order = Column(Integer, nullable=False, default=1)
    # The resolver config node this step ran, by id and by its name within the resolution's config_snapshot.
    action_resolver_config_id = Column(UUID(as_uuid=True), nullable=False)
    action_resolver_config_name = Column(String(255), nullable=False)
    status = Column(String(32), nullable=False, default="pending")
    result = Column(JSON, nullable=True)

    resolution = relationship("Resolution", back_populates="steps")


@event.listens_for(Session, "before_flush")
def _validate_relationship_participants(session, flush_context, instances):
    for obj in list(session.new) + list(session.dirty):
        if not isinstance(obj, RelationshipParticipant):
            continue

        relationship = obj.relationship_
        if relationship is None:
            relationship = session.get(Relationship, obj.relationship_id)
        if relationship is None:
            continue

        participant_type = obj.participant_type
        if participant_type is None:
            participant_type = session.get(ParticipantType, obj.participant_type_id)
        if participant_type is None:
            continue

        entity = obj.entity
        if entity is None:
            entity = session.get(Entity, obj.entity_id)
        if entity is None:
            raise ValueError("entity is required for relationship participant")

        role_bindings = [
            row
            for row in relationship.relationship_type.participant_types
            if row.participant_type_id == participant_type.id
        ]
        if not role_bindings:
            raise ValueError("participant type is not valid for this relationship type")

        allowed_entity_types = {row.entity_type_id for row in role_bindings if row.entity_type_id is not None}
        allowed_entity_classes = {row.entity_class_id for row in role_bindings if row.entity_class_id is not None}
        if entity.entity_type_id in allowed_entity_types:
            continue
        if allowed_entity_classes:
            entity_classes = {
                entity_class_id
                for (entity_class_id,) in session.query(EntityTypeClass.entity_class_id).filter_by(entity_type_id=entity.entity_type_id)
            }
            if entity_classes & allowed_entity_classes:
                continue
        raise ValueError("entity type does not match the allowed participant type or class for this relationship type")
