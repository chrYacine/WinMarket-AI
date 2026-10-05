"""Repository-level tests for ScoringPolicy/ProviderProfile — real SQLite
(via the `db`/`test_db` conftest fixtures, tables created from
src/web/database/models.py, same as every other B02/B03 repo test), no
HTTP layer. Exercises the versioning/activation state machine directly."""
from __future__ import annotations

import pytest

from tests.conftest import default_org_id, make_active_starter_user
from src.web.database.repositories import provider_profile as provider_profile_repo
from src.web.database.repositories import scoring_policy as scoring_policy_repo


VALID_WEIGHTS = {
    "Adequation expertise": 20, "References similaires": 15, "Disponibilite equipe": 10,
    "Rentabilite estimee": 10, "Faisabilite delai": 10, "Certifications requises": 10,
    "Complexite technique": 5, "Connaissance secteur": 5, "Potentiel commercial": 5,
    "Risque contractuel": 5, "Solidite client": 3, "Valeur strategique": 2,
}


def test_new_owner_has_no_provider_profile_and_no_scoring_policy(db):
    user = make_active_starter_user(db, "fresh@example.com", scoring=False)
    org_id = default_org_id(db, user)
    assert provider_profile_repo.get_for_owner(db, organization_id=org_id, owner_user_id=user.id) is None
    assert scoring_policy_repo.get_active(db, organization_id=org_id, owner_user_id=user.id) is None
    assert scoring_policy_repo.get_draft(db, organization_id=org_id, owner_user_id=user.id) is None
    assert scoring_policy_repo.list_versions(db, organization_id=org_id, owner_user_id=user.id) == []


def test_save_draft_creates_version_1_and_resumes_editing_in_place(db):
    user = make_active_starter_user(db, "draft@example.com", scoring=False)
    org_id = default_org_id(db, user)

    draft = scoring_policy_repo.save_draft(
        db, organization_id=org_id, owner_user_id=user.id, created_by_user_id=user.id,
        weights=VALID_WEIGHTS, threshold_go=88, threshold_sous_reserve=60,
    )
    assert draft.version == 1
    assert draft.status == "draft"

    updated_weights = dict(VALID_WEIGHTS)
    updated_weights["Valeur strategique"] = 10
    updated_weights["Adequation expertise"] = 12  # keep sum at 100
    draft_again = scoring_policy_repo.save_draft(
        db, organization_id=org_id, owner_user_id=user.id, created_by_user_id=user.id,
        weights=updated_weights, threshold_go=90, threshold_sous_reserve=65,
    )
    assert draft_again.id == draft.id, "resuming a draft must edit the SAME row, not create version 2"
    assert draft_again.version == 1
    assert draft_again.threshold_go == 90
    assert len(scoring_policy_repo.list_versions(db, organization_id=org_id, owner_user_id=user.id)) == 1


def test_activate_promotes_draft_and_archives_previous_active(db):
    user = make_active_starter_user(db, "activate@example.com", scoring=False)
    org_id = default_org_id(db, user)

    scoring_policy_repo.save_draft(
        db, organization_id=org_id, owner_user_id=user.id, created_by_user_id=user.id,
        weights=VALID_WEIGHTS, threshold_go=88, threshold_sous_reserve=60,
    )
    v1 = scoring_policy_repo.activate_draft(
        db, organization_id=org_id, owner_user_id=user.id, expected_active_version=None,
    )
    assert v1.status == "active"
    assert v1.activated_at is not None
    assert scoring_policy_repo.get_active(db, organization_id=org_id, owner_user_id=user.id).id == v1.id
    assert scoring_policy_repo.get_draft(db, organization_id=org_id, owner_user_id=user.id) is None

    # Editing again after activation must create a NEW version, never mutate v1.
    new_weights = dict(VALID_WEIGHTS)
    new_weights["Valeur strategique"] = 0
    new_weights["Adequation expertise"] = 22
    scoring_policy_repo.save_draft(
        db, organization_id=org_id, owner_user_id=user.id, created_by_user_id=user.id,
        weights=new_weights, threshold_go=88, threshold_sous_reserve=60,
    )
    v2 = scoring_policy_repo.activate_draft(
        db, organization_id=org_id, owner_user_id=user.id, expected_active_version=1,
    )
    assert v2.version == 2
    assert v2.status == "active"

    db.refresh(v1)
    assert v1.status == "archived", "activating v2 must archive v1, never leave two active rows"
    versions = scoring_policy_repo.list_versions(db, organization_id=org_id, owner_user_id=user.id)
    assert [v.version for v in versions] == [2, 1]
    assert [v.status for v in versions] == ["active", "archived"]


def test_save_draft_custom_criteria_round_trips_a_genuine_list_unchanged(db):
    user = make_active_starter_user(db, "customcriteria@example.com", scoring=False)
    org_id = default_org_id(db, user)
    criteria = [{"id": "zone_couverte", "fact_key": "zone_intervention", "operator": "list_coverage"}]

    draft = scoring_policy_repo.save_draft(
        db, organization_id=org_id, owner_user_id=user.id, created_by_user_id=user.id,
        weights=VALID_WEIGHTS, threshold_go=88, threshold_sous_reserve=60, custom_criteria=criteria,
    )
    assert draft.custom_criteria == criteria


def test_save_draft_custom_criteria_never_silently_mangles_a_malformed_shape(db):
    """Reviewer-caught regression: `list(custom_criteria)` used to accept
    ANY iterable — a client-sent dict {"id": "x"} silently became `['id']`
    (the dict's own keys), corrupting the draft with no error anywhere.
    The malformed shape must now round-trip exactly as given, so
    validate_custom_criteria's own isinstance check can reject it cleanly
    at /validate — never a repository-level reinterpretation."""
    user = make_active_starter_user(db, "malformedcriteria@example.com", scoring=False)
    org_id = default_org_id(db, user)
    malformed = {"id": "not-a-list"}

    draft = scoring_policy_repo.save_draft(
        db, organization_id=org_id, owner_user_id=user.id, created_by_user_id=user.id,
        weights=VALID_WEIGHTS, threshold_go=88, threshold_sous_reserve=60, custom_criteria=malformed,
    )
    assert draft.custom_criteria == malformed, "a malformed shape must round-trip unchanged, never be reinterpreted as a list of its keys"

    from src.agents.business_facts import validate_custom_criteria
    errors = validate_custom_criteria(draft.custom_criteria, known_facts={})
    assert any("liste" in e.lower() for e in errors)


def test_activate_with_stale_expected_version_raises_conflict(db):
    """Scenario 6 of the ticket's recipe: a second activation attempt that
    doesn't know a version was already activated must be refused, and the
    real active state must not change."""
    user = make_active_starter_user(db, "conflict@example.com", scoring=False)
    org_id = default_org_id(db, user)

    scoring_policy_repo.save_draft(
        db, organization_id=org_id, owner_user_id=user.id, created_by_user_id=user.id,
        weights=VALID_WEIGHTS, threshold_go=88, threshold_sous_reserve=60,
    )
    v1 = scoring_policy_repo.activate_draft(
        db, organization_id=org_id, owner_user_id=user.id, expected_active_version=None,
    )

    scoring_policy_repo.save_draft(
        db, organization_id=org_id, owner_user_id=user.id, created_by_user_id=user.id,
        weights=VALID_WEIGHTS, threshold_go=70, threshold_sous_reserve=50,
    )
    # Stale expectation: acts as if no policy were active yet (like the very
    # first activation), unaware that v1 already got there first.
    with pytest.raises(scoring_policy_repo.ActivationConflict) as excinfo:
        scoring_policy_repo.activate_draft(
            db, organization_id=org_id, owner_user_id=user.id, expected_active_version=None,
        )
    assert excinfo.value.current_active_version == 1

    db.refresh(v1)
    assert v1.status == "active", "the conflicting activation must leave the real active version untouched"
    draft = scoring_policy_repo.get_draft(db, organization_id=org_id, owner_user_id=user.id)
    assert draft is not None and draft.status == "draft", "a refused activation must leave the draft as a draft"


def test_activate_with_no_draft_raises_value_error(db):
    user = make_active_starter_user(db, "nodraft@example.com", scoring=False)
    org_id = default_org_id(db, user)
    with pytest.raises(ValueError):
        scoring_policy_repo.activate_draft(
            db, organization_id=org_id, owner_user_id=user.id, expected_active_version=None,
        )


def test_two_owners_never_share_a_scoring_policy(db):
    user_a = make_active_starter_user(db, "policy_a@example.com", scoring=False)
    user_b = make_active_starter_user(db, "policy_b@example.com", scoring=False)
    org_a = default_org_id(db, user_a)
    org_b = default_org_id(db, user_b)

    scoring_policy_repo.save_draft(
        db, organization_id=org_a, owner_user_id=user_a.id, created_by_user_id=user_a.id,
        weights=VALID_WEIGHTS, threshold_go=88, threshold_sous_reserve=60,
    )
    scoring_policy_repo.activate_draft(db, organization_id=org_a, owner_user_id=user_a.id, expected_active_version=None)

    assert scoring_policy_repo.get_active(db, organization_id=org_b, owner_user_id=user_b.id) is None
    assert scoring_policy_repo.list_versions(db, organization_id=org_b, owner_user_id=user_b.id) == []


def test_provider_profile_save_for_owner_never_writes_a_colleagues_row(db):
    user_a = make_active_starter_user(db, "profile_a@example.com", scoring=False)
    user_b = make_active_starter_user(db, "profile_b@example.com", scoring=False)
    org_a = default_org_id(db, user_a)
    org_b = default_org_id(db, user_b)

    provider_profile_repo.save_for_owner(
        db, organization_id=org_a, owner_user_id=user_a.id,
        raison_sociale="ESN de A", effectif="10-50", competences=["python", "aws"], certifications=[],
    )
    assert provider_profile_repo.get_for_owner(db, organization_id=org_b, owner_user_id=user_b.id) is None
    profile_a = provider_profile_repo.get_for_owner(db, organization_id=org_a, owner_user_id=user_a.id)
    assert profile_a.status == "complete"
    assert profile_a.competences == ["python", "aws"]


def test_provider_profile_incomplete_without_raison_sociale(db):
    user = make_active_starter_user(db, "incomplete@example.com", scoring=False)
    org_id = default_org_id(db, user)
    profile = provider_profile_repo.save_for_owner(
        db, organization_id=org_id, owner_user_id=user.id,
        raison_sociale=None, effectif=None, competences=[], certifications=[],
    )
    assert profile.status == "incomplete"
