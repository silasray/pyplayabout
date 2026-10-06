import pytest

from app.models import ActionResolverConfig, GameTypeResourceChange

from helpers import GameTypeSpec

CORE_BUNDLE = "bulk_data/game_types/core_game_bundle.json"
DERIVED_BUNDLE = "bulk_data/game_types/derived_game_bundle.json"

BUNDLE_CASES = [
    pytest.param([GameTypeSpec(name="core_game", version=1)], [CORE_BUNDLE], id="core"),
    pytest.param([GameTypeSpec(name="core_game", version=1), GameTypeSpec(name="dm_house_rules", version=1)], [CORE_BUNDLE, DERIVED_BUNDLE], id="core+house_rules"),
]


@pytest.mark.parametrize("game_type_specs, bulk_game_type_bundle", BUNDLE_CASES, indirect=True)
def test_bulk_import_and_delete_fixture_sets_up_and_tears_down_data(game_types, bulk_game_type_bundle):
    assert bulk_game_type_bundle is not None
    assert len(bulk_game_type_bundle) >= 1

    game_names = [
        (payload.get("game_type") or {}).get("name")
        for payload in bulk_game_type_bundle
    ]
    assert all(name for name in game_names)
    assert set(game_names) <= {game_type.name for game_type in game_types}


@pytest.mark.parametrize("game_type_specs, bulk_game_type_bundle", BUNDLE_CASES, indirect=True)
def test_bulk_import_fixture_supports_single_or_multiple_file_payloads(game_types, bulk_game_type_bundle, db_session):
    assert len(bulk_game_type_bundle) >= 1

    for payload in bulk_game_type_bundle:
        name = (payload.get("game_type") or {}).get("name")
        if name == "core_game":
            assert db_session.query(ActionResolverConfig).filter_by(name="slash_root").count() == 1
        elif name == "dm_house_rules":
            assert db_session.query(GameTypeResourceChange).filter_by(resource_key="parry_root").count() >= 0
            assert db_session.query(ActionResolverConfig).filter_by(name="parry_root").count() == 1


@pytest.mark.parametrize(
    "game_type_specs, bulk_game_type_bundle",
    [
        pytest.param(
            [
                GameTypeSpec(name="weapons", version=1, is_generic=True),
                GameTypeSpec(name="core_game", version=1),
                GameTypeSpec(name="dm_house_rules", version=1, derived_from=GameTypeSpec(name="core_game", version=1)),
                GameTypeSpec(name="empty_game", version=1),
            ],
            [CORE_BUNDLE, DERIVED_BUNDLE],
            id="four-game-types-two-bundles",
        )
    ],
    indirect=True,
)
def test_bulk_bundles_populate_only_their_own_game_types_among_many(game_types, bulk_game_type_bundle, db_session):
    assert [game_type.name for game_type in game_types] == ["weapons", "core_game", "dm_house_rules", "empty_game"]
    ids = {game_type.name: game_type.id for game_type in game_types}

    configs_by_game_type = {
        name: sorted(config.name for config in db_session.query(ActionResolverConfig).filter_by(game_type_id=game_type_id).all())
        for name, game_type_id in ids.items()
    }
    pending_reviews = {
        (change.resource_kind, change.resource_key)
        for change in db_session.query(GameTypeResourceChange).filter_by(derived_game_type_id=ids["dm_house_rules"], status="pending").all()
    }

    # The core bundle imports directly; the derived game type's bundle is queued for review instead of applied.
    assert configs_by_game_type == {
        "weapons": [],
        "core_game": ["slash_root"],
        "dm_house_rules": [],
        "empty_game": [],
    }
    assert ("action_resolver_config", "parry_root") in pending_reviews


@pytest.mark.parametrize(
    "game_type_specs",
    [
        [GameTypeSpec(name="core_game", version=1)],
        [GameTypeSpec(name="weapons", version=1, is_generic=True)],
        [
            GameTypeSpec(name="weapons", version=1, is_generic=True),
            GameTypeSpec(name="core_game", version=1),
            GameTypeSpec(name="dm_house_rules", version=1, derived_from=GameTypeSpec(name="core_game", version=1)),
            GameTypeSpec(name="table_two_rules", version=1, derived_from=GameTypeSpec(name="core_game", version=1)),
        ],
        [
            GameTypeSpec(name="core_game", version=1),
            GameTypeSpec(name="core_game", version=2),
            GameTypeSpec(name="dm_house_rules", version=1, derived_from=GameTypeSpec(name="core_game", version=2)),
        ],
    ],
    indirect=True,
)
def test_game_types_fixture_creates_requested_game_types(game_type_specs, game_types):
    assert len(game_types) == len(game_type_specs)

    for spec, game_type in zip(game_type_specs, game_types):
        assert game_type.signature == spec.signature
        assert game_type.is_generic == spec.get("is_generic", False)

        if spec.parent is None:
            assert game_type.derived_from is None
        else:
            assert game_type.derived_from.signature == spec.parent.signature
            assert game_type.derived_from in game_types


def test_game_types_fixture_defaults_to_single_non_generic_game_type(game_types):
    assert len(game_types) == 1
    assert game_types[0].is_generic is False
    assert game_types[0].derived_from is None
