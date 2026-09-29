# Action Resolution Design Notes

This file is a working design note for how the action-resolution system should be modeled as we evolve from a simple effect schema into a more open and composable rules engine.

## Goals

We want to support actions such as:

- `fireball`: deal damage, maybe create a fireball projectile, maybe apply burn status
- `equip`: create a relationship between an item and a character
- `open_chest`: roll a loot count, then spawn multiple items from a loot pool
- `cast_spell`: potentially chain a calculation and entity-spawn sequence

The core idea is that an action is not just one effect. It is a small resolution graph that can expand over time.

## Design direction

The current model has a useful distinction between:

- `ActionType`: the thing the user or system is asking to do
- `ActionResolverConfig`: the definition of how a specific action resolves

The missing extra layer is that a resolver config should be able to spawn follow-on resolvers, and those follow-on resolvers should be attached to a parent resolver in a polymorphic, tree-like structure.

That gives us an execution model like this:

- root resolver belongs to an `ActionType`
- child resolvers belong to a parent `ActionResolverConfig`
- child resolver relationships describe how the parent output feeds the child
- child resolvers can be of different effect kinds, such as calculation, entity mutation, or relationship creation

This allows a single action to produce a chain of runtime behavior without hard-coding every effect type into one giant `Effect` table.

## Proposed model structure

### 1. ActionType

`ActionType` stays as the top-level action classification.

Examples:

- `fireball`
- `equip`
- `open_chest`
- `attack`

This is the top-level nouns/verbs in the game system.

### 2. ActionResolverConfig

`ActionResolverConfig` becomes the recursive node in the action-resolution tree.

It has:

- `id`
- `name`
- `game_type_id`
- `action_type_id`
- `parent_id` (nullable)
- `relationship_type` (a simple string describing the relationship between parent and child, e.g. `child`, `follow_on`, `spawned_by`, etc.)

This structure allows us to model:

- the initial resolver for an action (`parent_id is null` and `action_type_id` set)
- follow-on resolvers spawned by the first resolver (`parent_id` points to the parent config)

This gives us a one-to-many tree rather than a single flat action config record.

### 3. Concrete effect subtypes

Each `ActionResolverConfig` can be associated with one concrete effect configuration record.

We separate effect kinds because the effect semantics differ substantially:

- calculation effects are formulas and numeric transforms
- entity mutation effects create, modify, destroy, or move entities
- relationship effects create or mutate relationships between entities

The concrete configuration subtypes are roughly:

- `EffectCalculationConfig` for `xdy+z` style rolls and other numeric modifiers
- `EntityMutationEffectConfig` for entity creation or destruction
- `RelationshipEffectConfig` for relationship creation or modification

This keeps effect behavior typed while still allowing runtime resolver chains.

## Why the parent/child chain matters

Consider the following action:

- `open_chest`
- effect 1: roll `2d6` for loot count
- effect 2: spawn that many item entities from the loot table

The runtime flow is not a single fixed record. It is a graph:

- root resolver: `open_chest`
- child resolver: `roll_loot_count`
- child of that: `spawn_loot_items`

The resolver graph can be represented as parent-child config records. The actual runtime execution can then walk these records and push newly generated participants/results into the next stage.

This model supports recursive resolution and makes it easier to model multi-step action chains without hard-coding every follow-on case into a giant monolithic effect table.

## Why a polymorphic effect config is still useful

The config is still meant to be somewhat normalized because different effect kinds have different fields.

For example:

- `EffectCalculationConfig` needs `dice_count`, `dice_sides`, `bonus`, `multiplier`
- `EntityMutationEffectConfig` needs `mutation_kind`, entity-type references, maybe `count_dice_count`
- `RelationshipEffectConfig` needs a target `relationship_type_id`

This lets the database validate the fields relevant to the effect kind without forcing all effect types into one broad table.

## How the BaseActionResolver fits in

The `BaseActionResolver` work still matters. It is the runtime execution contract for each resolver node.

At runtime, the resolver has access to:

- the resolution context
- the current pool of participants
- the current resolver config
- any output/results it produces

A resolver can then:

- consume participants
- push new participants into the next resolver queue
- create new relationship records or entity records
- emit a result object for later evaluation

This means that `BaseActionResolver` is not a replacement for the database model. It is the runtime execution layer that processes the structured config graph.

## Important caveat: open-ended systems need runtime safety

A resolver graph is flexible, but it introduces the usual risks:

- cycles
- deadlocks
- infinite expansion
- unbounded queued work

So once we move to this model, the runtime engine should probably enforce:

- a maximum number of resolver steps per resolution
- cycle detection or a visited-set of resolver config ids
- a priority/ordering field for child resolvers
- explicit parent/child semantics with validation rules for allowed relationships

This is not a reason to avoid the design. It is just the price of flexibility.

## Runtime history records

Each run of an action is recorded as history, separate from the static config model because config is declarative and runtime is stateful:

- `ActionResolverConfig` tells us what actions can do
- `Resolution*` tables tell us what happened in a concrete game state

The records are:

- `Resolution`: one run of an action
- `ResolutionParticipant`: an entity that took part, and in which role
- `ResolutionStep`: one resolver config node that ran, in order, with its structured JSON `result`

### History snapshots instead of links

History must stay accurate after the config and entities it describes change or are deleted, and the config tables are not versioned. So history records hold **no foreign keys to config or entity rows**, and changing or deleting config never cascades into them:

- `Resolution.config_snapshot` holds the config tree the run used (the root resolver config, its descendants, and the relationship types their effects create), serialized in the export format with a `snapshot_format_version`. It is stored once per resolution; each step names its node within the snapshot.
- `ResolutionParticipant.entity_snapshot` holds the entity's id, name and entity type.
- Original ids (config, action type, entity, participant type) are kept as plain columns for tracing back to live rows that still exist.

The one link out is `Resolution.game_type_id`. A game type with history is deprecated rather than deleted; see [game_type_lifecycle.md](game_type_lifecycle.md).

Snapshots are stored inline rather than in a shared, content-addressed snapshot table. If storage volume becomes a problem, inline snapshots can be moved to such a table (keyed by a hash of the canonical JSON) without changing their contents.

### Still to do

`run_action_resolution` (`app/resolution.py`) runs only the root resolver. Walking the resolver tree, passing results between steps, and the runtime safety limits above are future work.

## Summary

The idea is to move from a flat effect table to a recursive resolver tree plus typed effect sub-configs.

This gives us:

- the correct relationship between action and follow-on effect
- support for action chains without a huge relational explosion
- a way to represent calculation, mutation, and relationship semantics cleanly
- a foundation for a runtime resolver pipeline that can generate more work as it runs

