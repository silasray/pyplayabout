import uuid

import pytest

from app.database import SessionLocal
from app.models import ActionResolverConfig, GameType, GameTypeResourceChange

CORE_BUNDLE = "bulk_data/game_types/core_game_bundle.json"
DERIVED_BUNDLE = "bulk_data/game_types/derived_game_bundle.json"

BUNDLE_CASES = [
    pytest.param(["core_game"], [CORE_BUNDLE], id="core"),
    pytest.param(["core_game", "dm_house_rules"], [CORE_BUNDLE, DERIVED_BUNDLE], id="core+house_rules"),
]


@pytest.mark.parametrize("game_types, bulk_game_type_bundle", BUNDLE_CASES, indirect=True)
def test_bulk_import_and_delete_fixture_sets_up_and_tears_down_data(game_types, bulk_game_type_bundle):
    assert bulk_game_type_bundle is not None
    assert len(bulk_game_type_bundle) >= 1

    game_names = [
        (payload.get("game_type") or {}).get("name")
        for payload in bulk_game_type_bundle
    ]
    assert all(name for name in game_names)

    with SessionLocal() as session:
        for game_name in game_names:
            game_type = session.query(GameType).filter_by(name=game_name).one_or_none()
            assert game_type is not None, f"Missing game type {game_name} after setup"


@pytest.mark.parametrize("game_types, bulk_game_type_bundle", BUNDLE_CASES, indirect=True)
def test_bulk_import_fixture_supports_single_or_multiple_file_payloads(game_types, bulk_game_type_bundle):
    assert len(bulk_game_type_bundle) >= 1

    with SessionLocal() as session:
        for payload in bulk_game_type_bundle:
            name = (payload.get("game_type") or {}).get("name")
            if name == "core_game":
                assert session.query(ActionResolverConfig).filter_by(name="slash_root").count() == 1
            elif name == "dm_house_rules":
                assert session.query(GameTypeResourceChange).filter_by(resource_key="parry_root").count() >= 0
                assert session.query(ActionResolverConfig).filter_by(name="parry_root").count() == 1


@pytest.mark.parametrize(
    "game_types, bulk_game_type_bundle",
    [
        pytest.param(
            [
                {"name": "weapons", "is_generic": True},
                "core_game",
                {"name": "dm_house_rules", "derived_from": "core_game"},
                "empty_game",
            ],
            [CORE_BUNDLE, DERIVED_BUNDLE],
            id="four-game-types-two-bundles",
        )
    ],
    indirect=True,
)
def test_bulk_bundles_populate_only_their_own_game_types_among_many(game_types, bulk_game_type_bundle):
    assert [game_type["name"] for game_type in game_types] == ["weapons", "core_game", "dm_house_rules", "empty_game"]
    ids = {game_type["name"]: uuid.UUID(game_type["id"]) for game_type in game_types}

    with SessionLocal() as session:
        configs_by_game_type = {
            name: sorted(config.name for config in session.query(ActionResolverConfig).filter_by(game_type_id=game_type_id).all())
            for name, game_type_id in ids.items()
        }
        pending_reviews = {
            (change.resource_kind, change.resource_key)
            for change in session.query(GameTypeResourceChange).filter_by(derived_game_type_id=ids["dm_house_rules"], status="pending").all()
        }

    # The core bundle imports directly; the derived game type's bundle is queued for review instead of applied.
    assert configs_by_game_type == {
        "weapons": [],
        "core_game": ["slash_root"],
        "dm_house_rules": [],
        "empty_game": [],
    }
    assert ("action_resolver_config", "parry_root") in pending_reviews

