# SPDX-FileCopyrightText: 2026-present Tobias Kunze
# SPDX-License-Identifier: AGPL-3.0-only WITH LicenseRef-Pretalx-AGPL-3.0-Terms

from datetime import datetime
from decimal import Decimal

import pytest
from django.db import connection, transaction
from django_scopes import scopes_disabled

from pretalx.submission.domain.review import (
    create_or_update_review,
    update_review_score,
)
from pretalx.submission.domain.review_audit import (
    IDENTITY_NAME,
    RequestLedger,
    build_ledger_export,
    create_score_version,
    ensure_observation_start,
    ledger_enabled,
    observation_start,
)
from pretalx.submission.models import (
    ExposureKind,
    ExposureSource,
    Review,
    ReviewExposureBatch,
    ReviewExposureEntry,
    ReviewScoreVersion,
)
from tests.factories import (
    ReviewFactory,
    ReviewScoreCategoryFactory,
    ReviewScoreFactory,
    SpeakerFactory,
    SubmissionFactory,
    TeamFactory,
    TrackFactory,
    UserFactory,
)

pytestmark = [pytest.mark.unit, pytest.mark.django_db]


def enable_ledger(event):
    event.review_settings["review_exposure_ledger"] = True
    event.save(update_fields=["review_settings"])
    # ensure_observation_start updates the in-memory settings dict, so no
    # refresh is needed: refresh_from_db() would also drop the cached reverse
    # relations (e.g. cfp) and trip FETCH_MODE_RAISE when later factories
    # access event.cfp.
    return ensure_observation_start(event)


def make_reviewer(event):
    # Build the reviewer team with the FK id to avoid lazy related-object
    # fetches (blocked by FETCH_MODE_RAISE in tests).
    user = UserFactory()
    team = TeamFactory(
        organiser_id=event.organiser_id,
        all_events=True,
        is_reviewer=True,
        can_change_submissions=False,
    )
    team.members.add(user)
    return user


def ledger_for(event, user, source=ExposureSource.UI_SINGLE):
    return RequestLedger(event=event, viewer=user, source=source)


def default_score(event, value=1):
    category = event.score_categories.first()
    return category, category.scores.get(value=value)


# ---------------------------------------------------------------------------
# Enablement / observation start
# ---------------------------------------------------------------------------


def test_ledger_disabled_by_default(event):
    assert ledger_enabled(event) is False
    assert observation_start(event) is None


def test_observation_start_is_set_once_and_never_reset(event):
    start = enable_ledger(event)
    assert isinstance(start, str)
    assert ledger_enabled(event) is True
    first = datetime.fromisoformat(start)

    # Disable, then enable again: the start point stays immutable.
    event.review_settings["review_exposure_ledger"] = False
    event.save(update_fields=["review_settings"])
    assert ledger_enabled(event) is False

    event.review_settings["review_exposure_ledger"] = True
    event.save(update_fields=["review_settings"])
    ensure_observation_start(event)
    event.refresh_from_db()
    assert observation_start(event) == start
    assert datetime.fromisoformat(observation_start(event)) == first


# ---------------------------------------------------------------------------
# Batch / entry registration
# ---------------------------------------------------------------------------


def test_inactive_ledger_never_persists(event):
    reviewer = make_reviewer(event)
    submission = SubmissionFactory(event=event)
    ledger = ledger_for(event, reviewer)
    ledger.add_identity(submission, forms=[IDENTITY_NAME])
    # Viewer is a reviewer but the ledger is switched off.
    assert ledger.active is False
    assert ledger.commit(status_code=200) is None
    with scopes_disabled():
        assert ReviewExposureBatch.objects.count() == 0


def test_empty_active_ledger_creates_no_batch(event):
    enable_ledger(event)
    reviewer = make_reviewer(event)
    ledger = ledger_for(event, reviewer)
    assert ledger.active is True
    assert ledger.commit(status_code=200) is None
    with scopes_disabled():
        assert ReviewExposureBatch.objects.count() == 0


def test_batch_and_entries_persist_atomically(event):
    enable_ledger(event)
    reviewer = make_reviewer(event)
    submission = SubmissionFactory(event=event)
    other = UserFactory()
    review = ReviewFactory(submission=submission, user=other, text="Other words", score=2)

    ledger = ledger_for(event, reviewer)
    ledger.add_identity(submission, forms=[IDENTITY_NAME])
    ledger.add_other_review(
        submission, review_id=review.pk, has_text=True, has_score=True
    )
    batch = ledger.commit(status_code=200)

    assert batch is not None
    assert batch.viewer == reviewer
    assert batch.viewer_code == reviewer.code
    assert batch.source == ExposureSource.UI_SINGLE
    assert batch.status_code == 200
    with scopes_disabled():
        entries = ReviewExposureEntry.objects.filter(batch=batch).order_by("kind")
        kinds = set(entries.values_list("kind", flat=True))
        assert kinds == {
            ExposureKind.AUTHOR_IDENTITY,
            ExposureKind.OTHER_REVIEW,
        }
        other_entry = entries.get(kind=ExposureKind.OTHER_REVIEW)
        assert other_entry.details["reviews"][0]["review_id"] == review.pk
        assert other_entry.details["reviews"][0]["has_text"] is True
        assert other_entry.details["reviews"][0]["has_score"] is True


def test_own_and_empty_reviews_are_not_registered(event):
    enable_ledger(event)
    reviewer = make_reviewer(event)
    submission = SubmissionFactory(event=event)
    own = ReviewFactory(submission=submission, user=reviewer, text="mine", score=1)
    abstention = ReviewFactory(
        submission=submission, user=UserFactory(), text=None, score=None
    )

    ledger = ledger_for(event, reviewer)
    ledger.add_other_review(
        submission,
        review_id=own.pk,
        review_user_id=reviewer.pk,
        has_text=True,
        has_score=True,
    )
    ledger.add_other_review(
        submission,
        review_id=abstention.pk,
        review_user_id=abstention.user_id,
        has_text=False,
        has_score=False,
    )
    assert not ledger
    assert ledger.commit(status_code=200) is None
    with scopes_disabled():
        assert ReviewExposureBatch.objects.count() == 0


def test_half_batch_is_rolled_back(event, monkeypatch):
    enable_ledger(event)
    reviewer = make_reviewer(event)
    submission = SubmissionFactory(event=event)
    ledger = ledger_for(event, reviewer)
    ledger.add_identity(submission, forms=[IDENTITY_NAME])

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(ReviewExposureEntry.objects, "bulk_create", boom)
    with pytest.raises(RuntimeError, match="boom"):
        ledger.commit(status_code=200)
    with scopes_disabled():
        assert ReviewExposureBatch.objects.count() == 0
        assert ReviewExposureEntry.objects.count() == 0


# ---------------------------------------------------------------------------
# Score versions
# ---------------------------------------------------------------------------


def _submit(event, reviewer, submission, text, score_value=1, source="ui_single"):
    _, score = default_score(event, score_value)
    return create_or_update_review(
        submission=submission,
        user=reviewer,
        text=text,
        scores=[score.pk],
        source=source,
        event=event,
    )


def test_disabled_ledger_creates_no_versions(event):
    reviewer = make_reviewer(event)
    submission = SubmissionFactory(event=event)
    _submit(event, reviewer, submission, "first")
    with scopes_disabled():
        assert ReviewScoreVersion.objects.count() == 0


def test_identical_submissions_create_separate_immutable_versions(event):
    enable_ledger(event)
    reviewer = make_reviewer(event)
    submission = SubmissionFactory(event=event)

    review = _submit(event, reviewer, submission, "same text", score_value=1)
    review = _submit(event, reviewer, submission, "same text", score_value=1)

    with scopes_disabled():
        versions = list(
            ReviewScoreVersion.objects.filter(review=review).order_by("version_number")
        )
        assert len(versions) == 2
        assert [v.version_number for v in versions] == [1, 2]
        assert [v.text_snapshot for v in versions] == ["same text", "same text"]
        assert [v.source for v in versions] == ["ui_single", "ui_single"]
        choice = versions[0].score_choices_snapshot[0]
        assert choice["value"] == "1.00"
        assert choice["score_id"] is not None
        assert versions[0].total_score == Decimal("1.00")

    # Mutating the current review must not rewrite history.
    review.text = "changed"
    review.save()
    with scopes_disabled():
        first = ReviewScoreVersion.objects.get(version_number=1)
        assert first.text_snapshot == "same text"


def test_snapshot_counts_other_scores_not_own(event):
    enable_ledger(event)
    category = event.score_categories.first()
    score_one = category.scores.get(value=1)
    score_two = category.scores.get(value=2)

    reviewer = make_reviewer(event)
    submission = SubmissionFactory(event=event)

    # First version: nobody else has scored yet.
    review = create_or_update_review(
        submission=submission,
        user=reviewer,
        text="v1",
        scores=[score_one.pk],
        event=event,
    )
    with scopes_disabled():
        v1 = ReviewScoreVersion.objects.get(version_number=1)
    assert v1.display_score_snapshot["others_nonnull_count"] == 0
    assert v1.display_score_snapshot["median"] is None
    assert v1.display_score_snapshot["own_prior_score"] is None

    # Another reviewer now submits a non-null score.
    create_or_update_review(
        submission=submission,
        user=UserFactory(),
        text="other",
        scores=[score_two.pk],
        event=event,
    )

    # Reviewer edits: snapshot must show one other non-null score (median 2).
    create_or_update_review(
        submission=submission,
        user=reviewer,
        text="v2",
        scores=[score_one.pk],
        event=event,
    )
    with scopes_disabled():
        v2 = ReviewScoreVersion.objects.get(version_number=2)
    assert v2.display_score_snapshot["others_nonnull_count"] == 1
    assert v2.display_score_snapshot["median"] == "2.0"
    assert v2.display_score_snapshot["own_prior_score"] == "1.00"
    # The version binds review v1, whose prior score is recorded.
    assert v1.review_id == review.pk


def test_recalculation_does_not_create_versions(event):
    enable_ledger(event)
    reviewer = make_reviewer(event)
    submission = SubmissionFactory(event=event)
    review = _submit(event, reviewer, submission, "x")
    before = ReviewScoreVersion.objects.count()
    with scopes_disabled():
        update_review_score(review)
        assert ReviewScoreVersion.objects.count() == before


def test_versions_survive_phase_track_and_team_changes(event):
    enable_ledger(event)
    reviewer = make_reviewer(event)
    submission = SubmissionFactory(event=event)
    submission.speakers.add(SpeakerFactory(event=event))
    track = TrackFactory(event=event)
    _submit(event, reviewer, submission, "x")

    with scopes_disabled():
        # Phase settings change and deletion.
        phase = event.active_review_phase
        phase.can_see_other_reviews = "never"
        phase.save()
        phase.delete()
        # Track change on the proposal.
        submission.track = track
        submission.save()
        # Remove the reviewer from the team and delete the team.
        team = reviewer.teams.first()
        team.members.remove(reviewer)
        team.delete()

        assert ReviewScoreVersion.objects.count() == 1
        version = ReviewScoreVersion.objects.get()
        assert version.user_code == reviewer.code
        assert version.submission_code == submission.code


# ---------------------------------------------------------------------------
# Binding order: committed batches vs. versions
# ---------------------------------------------------------------------------


def test_version_binds_only_previously_committed_batches(event):
    enable_ledger(event)
    reviewer = make_reviewer(event)
    submission = SubmissionFactory(event=event)
    _, score = default_score(event, 1)

    # Version first (no prior exposure): unbound.
    review = create_or_update_review(
        submission=submission,
        user=reviewer,
        text="v1",
        scores=[score.pk],
        event=event,
    )
    v1 = ReviewScoreVersion.objects.get(version_number=1)
    assert set(v1.bound_batches.values_list("id", flat=True)) == set()

    # Then the reviewer is shown information: a batch commits.
    ledger = ledger_for(event, reviewer)
    ledger.add_identity(submission, forms=[IDENTITY_NAME])
    batch = ledger.commit(status_code=200)

    # And then the reviewer edits again: v2 binds the earlier batch.
    create_or_update_review(
        submission=submission,
        user=reviewer,
        text="v2",
        scores=[score.pk],
        event=event,
    )
    v2 = ReviewScoreVersion.objects.get(version_number=2)
    assert set(v2.bound_batches.values_list("id", flat=True)) == {batch.pk}
    # v1 stays unbound even retroactively.
    assert set(v1.bound_batches.values_list("id", flat=True)) == set()


def test_rolled_back_batch_is_never_bound(event):
    enable_ledger(event)
    reviewer = make_reviewer(event)
    submission = SubmissionFactory(event=event)
    _, score = default_score(event, 1)

    review = create_or_update_review(
        submission=submission,
        user=reviewer,
        text="v1",
        scores=[score.pk],
        event=event,
    )
    # A batch that attempts to persist and rolls back leaves nothing behind.
    ledger = ledger_for(event, reviewer)
    ledger.add_identity(submission, forms=[IDENTITY_NAME])
    try:
        with transaction.atomic():
            ledger.commit(status_code=200)
            raise RuntimeError("rollback")
    except RuntimeError:
        pass

    with scopes_disabled():
        assert ReviewExposureBatch.objects.count() == 0
    create_or_update_review(
        submission=submission,
        user=reviewer,
        text="v2",
        scores=[score.pk],
        event=event,
    )
    v2 = ReviewScoreVersion.objects.get(version_number=2, review=review)
    assert set(v2.bound_batches.values_list("id", flat=True)) == set()


def test_batch_created_after_version_in_same_transaction_is_not_bound(event):
    """The binding SELECT happens before any later insert, so even batches
    appearing later in the same transaction cannot be referenced."""
    enable_ledger(event)
    reviewer = make_reviewer(event)
    submission = SubmissionFactory(event=event)
    _, score = default_score(event, 1)

    review = create_or_update_review(
        submission=submission,
        user=reviewer,
        text="v1",
        scores=[score.pk],
        event=event,
    )
    with transaction.atomic():
        version = create_score_version(review=review, source="ui_single",
                                      event=event, submission=submission)
        ledger = ledger_for(event, reviewer)
        ledger.add_identity(submission, forms=[IDENTITY_NAME])
        later_batch = ledger.commit(status_code=200)
        assert later_batch is not None
    version.refresh_from_db()
    assert later_batch.pk not in set(
        version.bound_batches.values_list("id", flat=True)
    )


@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="Cross-connection READ COMMITTED visibility requires PostgreSQL",
)
@pytest.mark.django_db(transaction=True)
def test_concurrent_batch_commit_order_is_respected_postgres(event):
    import threading

    enable_ledger(event)
    reviewer = make_reviewer(event)
    submission = SubmissionFactory(event=event)
    _, score = default_score(event, 1)

    review = create_or_update_review(
        submission=submission,
        user=reviewer,
        text="v1",
        scores=[score.pk],
        event=event,
    )

    barrier = threading.Barrier(2)
    errors = []

    def commit_batch_after_barrier():
        try:
            from django.db import connections

            conn = connections.create_connection("default")
            conn.ensure_connection()
            barrier.wait(timeout=10)
            with conn.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO submission_reviewexposurebatch "
                    "(event_id, viewer_id, viewer_code, source, path, "
                    "request_method, status_code, page, created, updated) "
                    "VALUES (%s, %s, %s, %s, '', 'GET', 200, '', NOW(), NOW()) "
                    "RETURNING id",
                    [event.pk, reviewer.pk, reviewer.code, ExposureSource.UI_SINGLE],
                )
                batch_id = cursor.fetchone()[0]
                cursor.execute(
                    "INSERT INTO submission_reviewexposureentry "
                    "(batch_id, event_id, submission_id, submission_code, "
                    "position, kind, details, created, updated) "
                    "VALUES (%s, %s, %s, %s, 0, %s, '{}'::jsonb, NOW(), NOW())",
                    [
                        batch_id,
                        event.pk,
                        submission.pk,
                        submission.code,
                        ExposureKind.AUTHOR_IDENTITY,
                    ],
                )
            conn.commit()
            conn.close()
        except Exception as exc:  # pragma: no cover -- diagnostic only
            errors.append(exc)

    worker = threading.Thread(target=commit_batch_after_barrier)
    worker.start()

    from django.db import connections

    conn = connections["default"]
    # Open the version transaction, synchronise with the worker (its INSERT is
    # still uncommitted at the barrier), then take the binding snapshot before
    # the worker commits: that batch must not be visible/bound.
    with transaction.atomic():
        barrier.wait(timeout=10)
        version = create_score_version(
            review=review,
            source=ExposureSource.UI_SINGLE,
            event=event,
            submission=submission,
        )
        worker.join(timeout=10)
        bound = set(version.bound_batches.values_list("id", flat=True))
    assert not errors
    worker.join(timeout=5)
    # The worker commits concurrently around our snapshot; in no order may it
    # reference an uncommitted batch, and the version must always be internally
    # consistent (all bound batches exist).
    for batch_id in bound:
        assert ReviewExposureBatch.objects.filter(pk=batch_id).exists()


# ---------------------------------------------------------------------------
# Export document
# ---------------------------------------------------------------------------


def test_export_meta_unknown_before_enablement(event):
    data = build_ledger_export(event)
    assert data["meta"]["enabled"] is False
    assert data["meta"]["observation_start"] is None
    assert data["meta"]["history_status"] == "unknown"
    assert data["versions"] == []
    assert data["batches"] == []


def test_export_contains_versions_and_minimal_bound_evidence(event):
    enable_ledger(event)
    reviewer = make_reviewer(event)
    submission = SubmissionFactory(event=event)
    _, score = default_score(event, 1)
    create_or_update_review(
        submission=submission,
        user=reviewer,
        text="audit text",
        scores=[score.pk],
        event=event,
    )

    data = build_ledger_export(event)
    assert data["meta"]["enabled"] is True
    assert data["meta"]["observation_start"]
    version = data["versions"][0]
    assert version["user_code"] == reviewer.code
    assert version["submission_code"] == submission.code
    assert version["text_snapshot"] == "audit text"
    assert version["bound_exposures"] == []
    # No proposal material is copied.
    serialized = str(data)
    assert submission.title not in serialized


def test_extra_weighted_category_is_available(event):
    # Sanity: an independent category can exist and be reported in snapshots.
    independent = ReviewScoreCategoryFactory(event=event, is_independent=True)
    ind_score = ReviewScoreFactory(category=independent, value=3)
    enable_ledger(event)
    reviewer = make_reviewer(event)
    other = UserFactory()
    submission = SubmissionFactory(event=event)
    # Other reviewer gives an independent score.
    other_review = ReviewFactory(submission=submission, user=other)
    other_review.scores.add(ind_score)
    _, main_score = default_score(event, 1)
    create_or_update_review(
        submission=submission,
        user=reviewer,
        text="x",
        scores=[main_score.pk],
        event=event,
    )
    with scopes_disabled():
        version = ReviewScoreVersion.objects.get(version_number=1)
    independent_snapshot = version.display_score_snapshot["independent"]
    assert str(independent.pk) in independent_snapshot
    assert independent_snapshot[str(independent.pk)]["count"] == 1
