# SPDX-FileCopyrightText: 2026-present Tobias Kunze
# SPDX-License-Identifier: AGPL-3.0-only WITH LicenseRef-Pretalx-AGPL-3.0-Terms

"""End-to-end tests for the review information exposure ledger on the
built-in review UIs: single review page, bulk page and review dashboard.

The timeline asserted throughout is: score first (nothing to bind), then
information is displayed (a batch commits), then the reviewer modifies
their score (the new version binds exactly the previously committed
batches). All assertions use the real test client and the real database.
"""

import pytest
from django_scopes import scopes_disabled

from pretalx.submission.domain.review import update_review_score
from pretalx.submission.domain.review_audit import ensure_observation_start
from pretalx.submission.interfaces.forms import ReviewSettingsForm
from pretalx.submission.models import (
    ExposureKind,
    ExposureSource,
    ReviewExposureBatch,
    ReviewExposureEntry,
    ReviewScoreVersion,
)
from tests.factories import (
    EventFactory,
    ReviewFactory,
    SpeakerFactory,
    SubmissionFactory,
    UserFactory,
)
from tests.utils import make_orga_user

pytestmark = [pytest.mark.integration, pytest.mark.django_db]


def enable_ledger(event):
    with scopes_disabled():
        event.review_settings["review_exposure_ledger"] = True
        event.save(update_fields=["review_settings"])
        ensure_observation_start(event)


def make_reviewer(event):
    return make_orga_user(
        event,
        is_reviewer=True,
        can_change_submissions=False,
        can_change_organiser_settings=False,
    )


def set_phase(event, **values):
    with scopes_disabled():
        phase = event.active_review_phase
        for key, value in values.items():
            setattr(phase, key, value)
        phase.save()


def category_score(event, value=1):
    with scopes_disabled():
        category = event.score_categories.first()
        return category, category.scores.get(value=value)


def other_score(event, submission, user, score, *, text="Other reviewer text"):
    with scopes_disabled():
        review = ReviewFactory(submission=submission, user=user, text=text)
        review.scores.add(score)
        update_review_score(review)
        return review


def batches(viewer):
    with scopes_disabled():
        return list(
            ReviewExposureBatch.objects.filter(viewer=viewer)
            .prefetch_related("entries")
            .order_by("id")
        )


def entries_for(batch):
    return {(e.submission_code, e.kind): e for e in batch.entries.all()}


def versions(reviewer=None):
    with scopes_disabled():
        qs = ReviewScoreVersion.objects.prefetch_related("bound_batches").order_by(
            "version_number"
        )
        if reviewer is not None:
            qs = qs.filter(user=reviewer)
        return list(qs)


# ---------------------------------------------------------------------------
# Opt-in: disabled ledger changes nothing
# ---------------------------------------------------------------------------


def test_disabled_ledger_records_nothing(client, event):
    with scopes_disabled():
        reviewer = make_reviewer(event)
        submission = SubmissionFactory(event=event)
        submission.speakers.add(SpeakerFactory(event=event))
        category, score = category_score(event)
    client.force_login(reviewer)

    response = client.post(
        submission.orga_urls.reviews,
        data={f"score_{category.id}": score.id, "text": "LGTM"},
    )
    assert response.status_code == 302
    response = client.get(submission.orga_urls.reviews)
    assert response.status_code == 200

    with scopes_disabled():
        assert ReviewExposureBatch.objects.count() == 0
        assert ReviewExposureEntry.objects.count() == 0
        assert ReviewScoreVersion.objects.count() == 0


# ---------------------------------------------------------------------------
# Single review page: score -> display -> modify timeline
# ---------------------------------------------------------------------------


def test_single_page_score_first_then_display_then_modify_binds_batch(client, event):
    enable_ledger(event)
    with scopes_disabled():
        reviewer = make_reviewer(event)
        colleague = make_reviewer(event)
        submission = SubmissionFactory(event=event)
        submission.speakers.add(SpeakerFactory(event=event))
        category, score_one = category_score(event, 1)
        _, score_two = category_score(event, 2)
    set_phase(
        event,
        can_see_other_reviews="after_review",
        can_see_speaker_names=True,
        can_see_reviewer_names=True,
    )
    url = submission.orga_urls.reviews
    client.force_login(reviewer)

    # 1. Score first: version 1 exists but has no prior exposure to bind.
    response = client.post(
        url, data={f"score_{category.id}": score_one.id, "text": "first impression"}
    )
    assert response.status_code == 302
    v1 = versions(reviewer)[0]
    assert list(v1.bound_batches.all()) == []
    assert batches(reviewer) == []

    # 2. A colleague reviews, then the reviewer actually opens the page.
    other_score(event, submission, colleague, score_two)
    response = client.get(url)
    assert response.status_code == 200
    assert "Other reviewer text" in response.content.decode()
    [batch] = batches(reviewer)
    assert batch.source == ExposureSource.UI_SINGLE
    assert batch.status_code == 200
    entries = entries_for(batch)
    identity = entries[(submission.code, ExposureKind.AUTHOR_IDENTITY)]
    assert identity.details["forms"] == ["name"]
    other = entries[(submission.code, ExposureKind.OTHER_REVIEW)]
    assert other.details["reviews"][0]["has_text"] is True
    assert other.details["reviews"][0]["has_score"] is True
    assert other.details["reviews"][0]["attribution"] == "name"

    # 3. The reviewer modifies their review: version 2 binds the batch.
    response = client.post(
        url, data={f"score_{category.id}": score_one.id, "text": "revised opinion"}
    )
    assert response.status_code == 302
    v1, v2 = versions(reviewer)
    assert list(v1.bound_batches.all()) == []
    assert [b.pk for b in v2.bound_batches.order_by("id")] == [batch.pk]
    assert v1.text_snapshot == "first impression"
    assert v2.text_snapshot == "revised opinion"
    assert v2.display_score_snapshot["others_nonnull_count"] == 1
    assert v2.display_score_snapshot["median"] == "2.0"
    assert v2.display_score_snapshot["own_prior_score"] == "1.00"
    # The successful POST itself redirected and registered no batch.
    assert len(batches(reviewer)) == 1


def test_single_page_hidden_identity_records_neither_name_nor_attribution(
    client, event
):
    enable_ledger(event)
    with scopes_disabled():
        reviewer = make_reviewer(event)
        colleague = make_reviewer(event)
        submission = SubmissionFactory(event=event)
        speaker = SpeakerFactory(event=event, name="Unique Hidden Speaker Name")
        submission.speakers.add(speaker)
        _, score_two = category_score(event, 2)
    set_phase(
        event,
        can_see_other_reviews="always",
        can_see_speaker_names=False,
        can_see_reviewer_names=False,
    )
    other_score(event, submission, colleague, score_two)
    client.force_login(reviewer)

    response = client.get(submission.orga_urls.reviews)
    assert response.status_code == 200
    assert "Unique Hidden Speaker Name" not in response.content.decode()

    [batch] = batches(reviewer)
    kinds = {entry.kind for entry in batch.entries.all()}
    assert kinds == {ExposureKind.OTHER_REVIEW}
    entry = batch.entries.get(kind=ExposureKind.OTHER_REVIEW)
    assert entry.details["reviews"][0]["attribution"] is None
    assert entry.details["reviews"][0]["has_score"] is True


def test_single_page_200_validation_failure_registers_without_version(client):
    event = EventFactory(review_settings={"score_mandatory": True})
    enable_ledger(event)
    with scopes_disabled():
        reviewer = make_reviewer(event)
        submission = SubmissionFactory(event=event)
        submission.speakers.add(SpeakerFactory(event=event))
        category = event.score_categories.first()
    client.force_login(reviewer)

    # Invalid POST re-renders the page with 200: the actual output (speaker
    # names) is registered, but the rejected submission creates no version.
    response = client.post(
        submission.orga_urls.reviews,
        data={f"score_{category.id}": "", "text": "missing score"},
    )
    assert response.status_code == 200
    [batch] = batches(reviewer)
    assert batch.source == ExposureSource.UI_SINGLE
    assert versions(reviewer) == []

    # A valid POST redirects and creates the version, with no extra batch.
    score = category.scores.get(value=1)
    response = client.post(
        submission.orga_urls.reviews,
        data={f"score_{category.id}": score.id, "text": "now valid"},
    )
    assert response.status_code == 302
    assert len(batches(reviewer)) == 1
    assert len(versions(reviewer)) == 1


def test_abstain_and_skip_create_no_versions_or_batches(client, event):
    enable_ledger(event)
    with scopes_disabled():
        reviewer = make_reviewer(event)
        submission = SubmissionFactory(event=event)
        other_submission = SubmissionFactory(event=event)
    client.force_login(reviewer)

    response = client.post(
        submission.orga_urls.reviews, data={"review_submit": "abstain"}
    )
    assert response.status_code == 302
    response = client.post(
        other_submission.orga_urls.reviews, data={"review_submit": "skip_for_now"}
    )
    assert response.status_code == 302

    with scopes_disabled():
        assert submission.reviews.count() == 1  # abstention row exists
        assert ReviewScoreVersion.objects.count() == 0
        assert ReviewExposureBatch.objects.count() == 0


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


def test_dashboard_own_score_only_is_not_aggregate_other_score_is(client, event):
    enable_ledger(event)
    set_phase(event, can_see_other_reviews="always", can_see_speaker_names=True)
    with scopes_disabled():
        reviewer = make_reviewer(event)
        colleague = make_reviewer(event)
        own_only = SubmissionFactory(event=event)
        own_only.speakers.add(SpeakerFactory(event=event))
        other_scored = SubmissionFactory(event=event)
        other_scored.speakers.add(SpeakerFactory(event=event))
        category, score_one = category_score(event, 1)
        _, score_two = category_score(event, 2)
    client.force_login(reviewer)

    # Reviewer scores the first submission; nobody else scored anything.
    response = client.post(
        own_only.orga_urls.reviews,
        data={f"score_{category.id}": score_one.id, "text": "mine"},
    )
    assert response.status_code == 302
    response = client.get(event.orga_urls.reviews)
    assert response.status_code == 200
    [batch] = batches(reviewer)
    entries = entries_for(batch)
    assert (own_only.code, ExposureKind.AUTHOR_IDENTITY) in entries
    assert (other_scored.code, ExposureKind.AUTHOR_IDENTITY) in entries
    assert (own_only.code, ExposureKind.AGGREGATE_SCORE) not in entries
    assert (other_scored.code, ExposureKind.AGGREGATE_SCORE) not in entries

    # A colleague scores the second submission: the aggregate column now
    # contains another reviewer's non-null score.
    other_score(event, other_scored, colleague, score_two)
    response = client.get(event.orga_urls.reviews)
    assert response.status_code == 200
    second_batch = batches(reviewer)[-1]
    entries = entries_for(second_batch)
    aggregate = entries[(other_scored.code, ExposureKind.AGGREGATE_SCORE)]
    assert aggregate.details["others_nonnull_count"] == 1
    assert (own_only.code, ExposureKind.AGGREGATE_SCORE) not in entries


def test_dashboard_registers_only_the_current_page(client, event):
    enable_ledger(event)
    set_phase(event, can_see_speaker_names=True)
    with scopes_disabled():
        reviewer = make_reviewer(event)
        shared_speaker = SpeakerFactory(event=event)
        submissions = SubmissionFactory.create_batch(251, event=event)
        for submission in submissions:
            submission.speakers.add(shared_speaker)
    client.force_login(reviewer)
    url = event.orga_urls.reviews

    response = client.get(url)
    assert response.status_code == 200
    page_one = batches(reviewer)[0]
    assert page_one.source == ExposureSource.UI_DASHBOARD
    assert page_one.page == "1"
    page_one_codes = {
        entry.submission_code
        for entry in page_one.entries.filter(kind=ExposureKind.AUTHOR_IDENTITY)
    }
    assert len(page_one_codes) == 250

    response = client.get(url + "?page=2")
    assert response.status_code == 200
    page_two = batches(reviewer)[-1]
    assert page_two.page == "2"
    page_two_codes = {
        entry.submission_code
        for entry in page_two.entries.filter(kind=ExposureKind.AUTHOR_IDENTITY)
    }
    assert len(page_two_codes) == 1
    assert page_one_codes.isdisjoint(page_two_codes)

    # Pages past the end 404 and register nothing.
    before = len(batches(reviewer))
    response = client.get(url + "?page=3")
    assert response.status_code == 404
    assert len(batches(reviewer)) == before


# ---------------------------------------------------------------------------
# Bulk review page and HTMX row fragments
# ---------------------------------------------------------------------------


def test_bulk_page_and_fragments_bind_versions_in_commit_order(client, event):
    enable_ledger(event)
    set_phase(event, can_see_speaker_names=True)
    with scopes_disabled():
        reviewer = make_reviewer(event)
        submission = SubmissionFactory(event=event)
        submission.speakers.add(SpeakerFactory(event=event))
        another = SubmissionFactory(event=event)  # no speaker attached
        category, score_one = category_score(event, 1)
        _, score_two = category_score(event, 2)
    client.force_login(reviewer)
    bulk_url = event.orga_urls.reviews + "bulk/"

    response = client.get(bulk_url)
    assert response.status_code == 200
    b_get = batches(reviewer)[0]
    # Only the row with a speaker registers identity.
    assert {e.submission_code for e in b_get.entries.all()} == {submission.code}

    data_one = {
        "_submission": submission.code,
        f"{submission.code}-score_{category.id}": score_one.id,
        f"{submission.code}-text": "bulk v1",
    }
    response = client.post(bulk_url, data=data_one, headers={"HX-Request": "true"})
    assert response.status_code == 200
    v1 = versions(reviewer)[0]
    assert v1.source == ExposureSource.UI_BULK
    # The GET batch was committed before the save and is bound; the fragment
    # rendered by THIS response is only committed afterwards and is not.
    assert [b.pk for b in v1.bound_batches.order_by("id")] == [b_get.pk]
    b_frag_one = batches(reviewer)[-1]
    assert b_frag_one.pk != b_get.pk

    # Resubmitting identical data saves nothing and creates no version, but
    # the 200 fragment still shows (and registers) the row.
    response = client.post(bulk_url, data=data_one, headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert len(versions(reviewer)) == 1
    b_frag_two = batches(reviewer)[-1]

    # Changing the review binds every batch committed in the meantime.
    response = client.post(
        bulk_url,
        data={
            "_submission": submission.code,
            f"{submission.code}-score_{category.id}": score_two.id,
            f"{submission.code}-text": "bulk v2",
        },
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    v2 = versions(reviewer)[-1]
    assert [b.pk for b in v2.bound_batches.order_by("id")] == sorted(
        [b_get.pk, b_frag_one.pk, b_frag_two.pk]
    )

    # Unknown submission: 404 fragment registers nothing.
    before = len(batches(reviewer))
    response = client.post(
        bulk_url, data={"_submission": "NOPE"}, headers={"HX-Request": "true"}
    )
    assert response.status_code == 404
    assert len(batches(reviewer)) == before


# ---------------------------------------------------------------------------
# Settings toggle
# ---------------------------------------------------------------------------


def _settings_data(**overrides):
    data = {
        "score_mandatory": False,
        "text_mandatory": False,
        "score_format": "words_numbers",
        "aggregate_method": "median",
        "review_help_text_0": "",
        "use_submission_comments": True,
    }
    data.update(overrides)
    return data


def test_settings_form_toggle_stamps_start_once(client, event):
    form = ReviewSettingsForm(
        data=_settings_data(review_exposure_ledger=True), obj=event, initial={}
    )
    assert form.is_valid(), form.errors
    form.save()
    event.refresh_from_db()
    assert event.review_settings["review_exposure_ledger"] is True
    start = event.review_settings["review_exposure_ledger_started"]
    assert start

    # Disable: the ledger switches off but the observation start remains.
    form = ReviewSettingsForm(
        data=_settings_data(review_exposure_ledger=False), obj=event, initial={}
    )
    assert form.is_valid(), form.errors
    form.save()
    event.refresh_from_db()
    assert event.review_settings["review_exposure_ledger"] is False
    assert event.review_settings["review_exposure_ledger_started"] == start

    # Re-enable: no new start point is stamped.
    form = ReviewSettingsForm(
        data=_settings_data(review_exposure_ledger=True), obj=event, initial={}
    )
    assert form.is_valid(), form.errors
    form.save()
    event.refresh_from_db()
    assert event.review_settings["review_exposure_ledger_started"] == start


# ---------------------------------------------------------------------------
# History retention across phase/track/team changes
# ---------------------------------------------------------------------------


def test_ledger_history_survives_phase_track_and_team_changes(client, event):
    enable_ledger(event)
    with scopes_disabled():
        reviewer = make_reviewer(event)
        submission = SubmissionFactory(event=event)
        submission.speakers.add(SpeakerFactory(event=event))
        category, score_one = category_score(event, 1)
    client.force_login(reviewer)
    client.post(
        submission.orga_urls.reviews,
        data={f"score_{category.id}": score_one.id, "text": "retain me"},
    )
    client.get(submission.orga_urls.reviews)

    with scopes_disabled():
        version = ReviewScoreVersion.objects.get()
        batch = ReviewExposureBatch.objects.get()
        # Phase configuration changes.
        phase = event.active_review_phase
        phase.can_see_other_reviews = "never"
        phase.save()
        # Track configuration changes on the proposal.
        from tests.factories import TrackFactory

        submission.track = TrackFactory(event=event)
        submission.save()
        # The reviewer leaves the team and the team is deleted.
        team = reviewer.teams.first()
        team.members.remove(reviewer)
        team.delete()

        # All audit rows survive, with their redundant codes intact even
        # though the viewer FK is now SET_NULL.
        assert ReviewScoreVersion.objects.filter(pk=version.pk).exists()
        batch.refresh_from_db()
        assert batch.viewer_id is None
        assert batch.viewer_code == reviewer.code
        entry = ReviewExposureEntry.objects.get(batch=batch)
        assert entry.submission_code == submission.code
        version.refresh_from_db()
        assert version.user_code == reviewer.code
        assert version.text_snapshot == "retain me"
        assert list(version.bound_batches.all()) == [batch]
