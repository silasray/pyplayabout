# Game Type Lifecycle

This note covers how game types are derived, deprecated, deleted and addressed, and the follow-up work those rules leave open.

## Derived game types

A derived game type represents the ruleset of a game in progress. It inherits from its ancestor with an overwrite-if-exists pattern:

- **House rules** are explicit overwrites made in the derived game type. A resource defined there replaces the inherited resource of the same kind and name.
- **Incoming changes from ancestors** are never applied directly. They are queued as `GameTypeResourceChange` rows and must be accepted or rejected through the review endpoints, so rules don't change mid-session without explicit acknowledgement.

> Resolving a derived game type's effective resources through its ancestors (the "live lookup" side of inheritance) is not implemented yet. Today a derived game type holds its own resources plus the review queue.

### Derivation depth

Derivation is currently limited to **one level**: a game type can't be derived from a game type that is itself derived. Deeper chains raise questions of ownership and permissions (who approves which ancestor's changes) that haven't been designed yet.

The limit is enforced only in the create flow (`MAX_DERIVATION_DEPTH` in `app/config_api.py`). Everything else, such as walking ancestors and descendants during deprecation, handles chains of any depth, so lifting the limit should only need that one check changed. **This needs to be revisited.**

## The deprecated state

A game type is **active** while `deprecated_at` is null and **deprecated** once it holds a timestamp (UTC).

Deprecating a game type keeps its row and all of its config, so history records and derived game types that reference it stay valid, and frees its name and version for a new active game type. Uniqueness is enforced by two partial indexes:

- active game types are unique by `(name, version)`
- deprecated game types are unique by `(name, version, deprecated_at)`

### Visibility rules

Deprecated game types are hidden by default and only appear when explicitly asked for or when something links to them. Every game type getter returns a list, empty when nothing matches, so the `include_deprecated` flag means the same thing at every level of the path. Filters live in the path; a getter rejects any query parameter it does not declare with a 400.

| Context | Deprecated game types |
|---|---|
| `GET /game-types`, `GET /game-types/{name}`, `GET /game-types/{name}/versions/{version}` | excluded unless `?include_deprecated=true` |
| `GET /game-types/{name}/versions/{version}/deprecated/{deprecated_at}` | listed; `deprecated_at` is the ISO 8601 value from the game type's representation |
| `GET /game-types/by-id/{id}` | listed |
| A resource that links to a game type (e.g. a derived game type's `derived_from`) | always returned in full |
| Every other endpoint addressed by name and version (deprecate, delete, and all config endpoints) | never matched |

When a GraphQL interface is added, it should mirror this: resolve by UUID as an alternate path, accept `deprecated_at` to address a deprecated game type, and take an explicit flag to include deprecated game types in collection queries.

## Deprecate and delete

**`POST /game-types/{name}/versions/{version}/deprecate`** deprecates an active game type and queues an `ancestor_deprecated` change (`resource_kind` `game_type`) for review on every active descendant.

**`DELETE /game-types/{name}/versions/{version}`** is explicit and never cascades:

1. If any config still belongs to the game type (entity types, action types, relationship types, ...), the delete is refused with a 409 listing it. Config must be deleted first.
2. Otherwise, if history records (`resolution`, `game_type_resource_change`) or derived game types still reference it, it is **deprecated instead of deleted**, exactly as by the deprecate endpoint, and the 200 response carries a `deprecated_instead_of_deleted` warning listing what prevented the delete.
3. Otherwise it is deleted.

**`DELETE /game-types/{name}/versions/{version}/deprecated/{deprecated_at}`** permanently deletes a deprecated game type once nothing references it.

## Rebase (not implemented)

Deleting or deprecating an ancestor is a mass change-while-in-flight for every derived game type. It goes through the ancestor-change review flow as a single `ancestor_deprecated` change rather than one change per inherited resource.

Accepting that change should **rebase** the derived game type: swap a new game type into its ancestry as a whole, instead of copying resources over piece by piece. Until the rebase feature exists, accepting or rejecting the change only records the acknowledgement. `TODO` comments mark the places in `app/config_api.py` that will need it (`_deprecate_game_type` and the review accept endpoint).

## Other follow-ups

- There is no endpoint for deleting history records, so a game type with history can be deprecated but never deleted.
- A derived game type's own review queue counts as history, so a derived game type that has received ancestor changes is deprecated rather than deleted.
