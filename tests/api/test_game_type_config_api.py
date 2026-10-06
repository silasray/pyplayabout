from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models import (
    ActionResolverConfig,
    ActionType,
    EffectCalculationConfig,
    EntityType,
    GameType,
    GameTypeResourceChange,
    RelationshipType,
)

client = TestClient(app)


def test_export_endpoint_returns_game_type_config():
    with SessionLocal() as session:
        game_type = GameType(name="alpha", version=1, is_generic=False)
        session.add(game_type)
        session.flush()

        entity_type = EntityType(name="sword", game_type_id=game_type.id)
        session.add(entity_type)
        session.flush()

        action_type = ActionType(name="strike", game_type_id=game_type.id)
        session.add(action_type)
        session.flush()

        config = ActionResolverConfig(
            name="strike_root",
            resolver_key="example_runtime_resolver",
            game_type_id=game_type.id,
            action_type_id=action_type.id,
            relationship_type="root",
        )
        session.add(config)
        session.flush()

        session.add(
            EffectCalculationConfig(
                action_resolver_config_id=config.id,
                dice_count=1,
                dice_sides=6,
                bonus=1,
                multiplier=1,
            )
        )
        session.flush()
        session.commit()

    response = client.post("/api/v1/game-types/alpha/versions/1/config/export")
    assert response.status_code == 200
    data = response.json()
    assert data["game_type"]["name"] == "alpha"
    assert data["game_type"]["version"] == 1
    assert any(item["name"] == "strike_root" for item in data["action_resolver_configs"])


def test_import_preview_requires_confirmation_for_cross_game_or_update_conflict():
    with SessionLocal() as session:
        session.add(GameType(name="alpha", version=1, is_generic=False))
        session.commit()

    response = client.post(
        "/api/v1/game-types/alpha/versions/1/config/import/preview",
        json={
            "game_type": {"name": "beta"},
            "entity_types": [{"name": "sword"}],
            "action_resolver_configs": [{"name": "strike_root", "resolver_key": "example_runtime_resolver"}],
        },
    )
    assert response.status_code == 409
    body = response.json()
    assert body["warning_type"] in {"key_collision", "update_required", "game_type_mismatch"}


def test_delete_preview_requires_explicit_confirmation_for_cascade_risk():
    with SessionLocal() as session:
        game_type = GameType(name="alpha", version=1, is_generic=False)
        session.add(game_type)
        session.flush()
        relationship_type = RelationshipType(name="attacks", game_type_id=game_type.id)
        session.add(relationship_type)
        session.flush()
        session.commit()

    response = client.post(
        "/api/v1/game-types/alpha/versions/1/config/delete/preview",
        json={"relationship_types": [{"name": "attacks"}]},
    )
    assert response.status_code == 409
    body = response.json()
    assert body["warning_type"] == "cascade_warning"


def test_copy_endpoint_copies_selected_resources_from_generic_game_type_to_target():
    with SessionLocal() as session:
        source = GameType(name="weapon_library", version=1, is_generic=True)
        target = GameType(name="alpha", version=1, is_generic=False)
        session.add_all([source, target])
        session.flush()

        action_type = ActionType(name="slash", game_type_id=source.id)
        session.add(action_type)
        session.flush()

        config = ActionResolverConfig(
            name="slash_root",
            resolver_key="example_runtime_resolver",
            game_type_id=source.id,
            action_type_id=action_type.id,
            relationship_type="root",
        )
        session.add(config)
        session.flush()
        session.add(
            EffectCalculationConfig(
                action_resolver_config_id=config.id,
                dice_count=2,
                dice_sides=6,
                bonus=1,
                multiplier=1,
            )
        )
        session.commit()

    response = client.post(
        "/api/v1/game-types/alpha/versions/1/config/copy",
        json={
            "source_game_type": {"name": "weapon_library", "version": 1},
            "action_types": [{"name": "slash"}],
            "action_resolver_configs": [{"name": "slash_root"}],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["copied"]["action_types"] == ["slash"]
    assert body["copied"]["action_resolver_configs"] == ["slash_root"]

    with SessionLocal() as session:
        copied_config = session.query(ActionResolverConfig).filter_by(name="slash_root", game_type_id=session.query(GameType).filter_by(name="alpha", version=1).one().id).one()
        assert copied_config.action_type.name == "slash"
        assert copied_config.effect_config.dice_count == 2


def test_derived_game_type_import_is_queued_as_pending_review_instead_of_live_update():
    with SessionLocal() as session:
        parent = GameType(name="core_game", version=1, is_generic=False)
        derived = GameType(name="dm_house_rules", version=1, is_generic=False, derived_from=parent)
        session.add_all([parent, derived])
        session.flush()
        session.commit()

    response = client.post(
        "/api/v1/game-types/dm_house_rules/versions/1/config/import",
        json={
            "entity_types": [{"name": "sword"}],
            "action_resolver_configs": [
                {
                    "name": "slash_root",
                    "resolver_key": "example_runtime_resolver",
                    "action_type": "slash",
                    "relationship_type": "root",
                    "effect_config": {"kind": "calculation", "dice_count": 2, "dice_sides": 6, "bonus": 1, "multiplier": 1},
                }
            ],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "pending_review"
    assert body["changes"][0]["resource_kind"] == "entity_type"

    with SessionLocal() as session:
        review_rows = session.query(GameTypeResourceChange).filter_by(derived_game_type_id=session.query(GameType).filter_by(name="dm_house_rules", version=1).one().id).all()
        assert len(review_rows) >= 2
        assert {row.status for row in review_rows} == {"pending"}
        assert session.query(ActionResolverConfig).filter_by(name="slash_root").count() == 0


def test_derived_game_type_review_accept_updates_record_status():
    with SessionLocal() as session:
        parent = GameType(name="core_game", version=1, is_generic=False)
        derived = GameType(name="dm_house_rules", version=1, is_generic=False, derived_from=parent)
        session.add_all([parent, derived])
        session.flush()
        change = GameTypeResourceChange(
            derived_game_type_id=derived.id,
            source_game_type_id=parent.id,
            resource_kind="entity_type",
            resource_key="sword",
            status="pending",
            snapshot_payload='{"name": "sword"}',
        )
        session.add(change)
        session.commit()
        change_id = str(change.id)

    response = client.post(f"/api/v1/game-types/dm_house_rules/versions/1/config/review/{change_id}/accept")
    assert response.status_code == 200
    body = response.json()
    assert body["updated_status"] == "accepted"

    with SessionLocal() as session:
        saved_change = session.query(GameTypeResourceChange).filter_by(resource_key="sword").one()
        assert saved_change.status == "accepted"
