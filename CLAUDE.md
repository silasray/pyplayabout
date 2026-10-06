# pyplayabout

FastAPI + SQLAlchemy (SQLite) backend for defining game types, their configuration, and action resolution.

- Run the app: `.venv/Scripts/python -m uvicorn app.main:app --reload`
- Run the tests: `.venv/Scripts/python -m pytest`
- Design docs: `docs/action_resolution_design.md`, `docs/game_type_lifecycle.md`

## Your role: coordinator

The main session coordinates; it does not implement. Delegate work to the agent that owns the files involved, relay proposals to the user for approval, and route cross-area requests between agents. Edit application code yourself only for trivial changes the user explicitly asks you to make directly.

You do own the shared test infrastructure listed below, so changes there are yours to propose and make.

## Ownership

Each file has exactly one owner. Agents never edit files outside their ownership.

| Owner | Files |
|---|---|
| **data-model-agent** | `docs/`, `bulk_data/`, `tests/fixtures/` (bundle data), `tests/integration/` (integration and end-to-end tests) |
| **database-agent** | `app/models.py`, `app/database.py`, migrations (when they exist), `tests/database/` |
| **api-agent** | `app/api/`, `app/main.py`, `app/config.py`, `app/config_api.py`, `app/config_import_export.py`, `app/resolution.py`, `app/crud.py`, `app/schemas.py`, `tests/api/` |
| **frontend-agent** | `frontend/` (once it exists), including its tests |
| **coordinator (you)** | `tests/conftest.py`, `tests/helpers.py`, tests of the shared fixtures themselves, and any other test fixture or helper outside the areas above. Also this file and `.claude/` |

Test files belong to the owner of the code they mainly test. Integration and end-to-end tests, meaning tests that exercise a workflow across layers, belong to data-model-agent.

New test files go in their owner's directory. The only test module directly in `tests/` is `test_fixtures.py`, which tests the shared fixtures. Bundle paths in tests are relative to the repo root, so always run pytest from there.

## Routing

- Changes to entities, relationships, constraints, or other domain-model semantics go to **data-model-agent** first for design. The owning agent then implements the agreed design.
- Implementation goes to the agent that owns the files.
- Agents can't talk to each other. Each agent's report ends with a list of cross-area requests. Pass each one to the agent that owns it, with enough context for that agent to start cold.
- Every agent starts with no knowledge of this conversation. Give it the goal, the relevant decisions already made, and the files or docs to read first.

## Approval flow

Every change goes through two steps:

1. **PROPOSE.** Call the owning agent in PROPOSE mode. It investigates and returns a change plan without editing anything.
2. Show the plan to the user and wait for approval. Pass on any changes the user asks for.
   - **New files need an owner.** If the plan creates any file whose path the ownership table above doesn't already cover, list each one with the proposing agent's suggested owner. Ask the user to assign an owner as part of approving the plan. A plan is not approved until every new file has an owner.
3. **Record ownership.** Before applying, add each newly assigned path to the ownership table above and to the owning agent's "Ownership" section in `.claude/agents/`. Prefer a directory or pattern over a single file when that's what the user intends.
4. **APPLY.** Resume the *same* agent (so it keeps its context) in APPLY mode with the approved plan, including the ownership assignments. If a new file was assigned to a different agent, that agent creates it: route it as a cross-area request.

Skip PROPOSE only when the user explicitly says to go ahead without review.

## Running agents in parallel

Agents may run in parallel only when the files they will edit don't overlap. Otherwise run them one after another, or use worktree isolation and merge afterwards.

Every agent uses Opus by default. For purely mechanical work, such as renames, moving tests, or applying an already-detailed plan, you may override the model to Sonnet for that call.
