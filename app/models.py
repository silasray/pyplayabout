import uuid

from sqlalchemy import Boolean, CheckConstraint, Column, Integer, String, ForeignKey, UniqueConstraint, not_
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import relationship

from app.database import Base


class GameType(Base):
    __tablename__ = "game_type"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    name = Column(String(255), nullable=False, unique=True)
    version = Column(Integer, nullable=False, default=1)
    # generic game types are used for creating entity type collections, ex "plants" or "weapons" for game makers to draw from when creating new systems.
    is_generic = Column(Boolean, nullable=False, default=False)


# entity_type is ex "long sword", while entity_class is ex "weapon".  Class membership is optional for types, and a type can belong to multiple classes.
class EntityType(Base):
    __tablename__ = "entity_type"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    name = Column(String(255), nullable=False, unique=True)
    entity_type_classes = relationship("EntityTypeClass", back_populates="entity_type")


class EntityClass(Base):
    __tablename__ = "entity_class"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    name = Column(String(255), nullable=False, unique=True)


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

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    name = Column(String(255), nullable=False, unique=True)


class EffectCalculationConfig(Base):
    __tablename__ = "effect_calculation_config"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    dice_count = Column(Integer, nullable=False)
    dice_sides = Column(Integer, nullable=False)
    bonus = Column(Integer, nullable=False, default=0)
    multiplier = Column(Integer, nullable=False, default=1)
    effect_type_id = Column(UUID(as_uuid=True), ForeignKey("effect_type.id"), nullable=False)
    effect_type = relationship("EffectType")
    subject_bindings = relationship(
        "EffectCalculationConfigTarget",
        primaryjoin="and_(EffectCalculationConfig.id == EffectCalculationConfigTarget.effect_calculation_config_id, "
        "EffectCalculationConfigTarget.is_subject == true())",
        foreign_keys="[EffectCalculationConfigTarget.effect_calculation_config_id]",
        cascade="all, delete-orphan",
    )
    object_bindings = relationship(
        "EffectCalculationConfigTarget",
        primaryjoin="and_(EffectCalculationConfig.id == EffectCalculationConfigTarget.effect_calculation_config_id, "
        "EffectCalculationConfigTarget.is_subject == false())",
        foreign_keys="[EffectCalculationConfigTarget.effect_calculation_config_id]",
        cascade="all, delete-orphan",
    )


class EffectCalculationConfigTarget(Base):
    __tablename__ = "effect_calculation_config_target"
    __table_args__ = (
        UniqueConstraint(
            "effect_calculation_config_id",
            "game_type_id",
            "is_subject",
            "target_type",
            "entity_type_id",
            "entity_class_id",
            name="uq_effect_config_target_game_subject",
        ),
        CheckConstraint(
            "((entity_type_id IS NOT NULL) <> (entity_class_id IS NOT NULL))",
            name="ck_effect_config_target_exactly_one_type",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    effect_calculation_config_id = Column(UUID(as_uuid=True), ForeignKey("effect_calculation_config.id"), nullable=False)
    game_type_id = Column(UUID(as_uuid=True), ForeignKey("game_type.id"), nullable=False)
    is_subject = Column(Boolean, nullable=False)
    target_type = Column(String(32), nullable=False)  # entity_type or entity_class
    entity_type_id = Column(UUID(as_uuid=True), ForeignKey("entity_type.id"), nullable=True)
    entity_class_id = Column(UUID(as_uuid=True), ForeignKey("entity_class.id"), nullable=True)

    @hybrid_property
    def is_object(self):
        return not self.is_subject

    @is_object.setter
    def is_object(self, value):
        self.is_subject = not value

    @is_object.expression
    def is_object(cls):
        return not_(cls.is_subject)

    effect_calculation_config = relationship(
        "EffectCalculationConfig",
        foreign_keys=[effect_calculation_config_id],
    )
    game_type = relationship("GameType")
    entity_type = relationship("EntityType")
    entity_class = relationship("EntityClass")


class CharacterType(Base):
    __tablename__ = "character"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    game_type_id = Column(UUID(as_uuid=True), ForeignKey("game_type.id"), nullable=False)
    game_type = relationship("GameType")



