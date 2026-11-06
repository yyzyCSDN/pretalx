# SPDX-FileCopyrightText: 2026-present Tobias Kunze
# SPDX-License-Identifier: AGPL-3.0-only WITH LicenseRef-Pretalx-AGPL-3.0-Terms

"""Domain service for the review information exposure ledger.

The ledger has two sides:

* Exposure batches/entries record what a logged-in reviewer was actually
  shown in one 2xx response (author identity, other reviewers' reviews or
  scores, aggregate scores containing other reviewers' non-null scores).
* Review score versions are immutable snapshots of accepted human review
  submissions, bound to the exposure batches for the same reviewer and
  proposal that were committed to the database before the version's own
  transaction committed.

Concurrency is defined purely by database commit order, which is verifiable
through the auto-incrementing primary keys: a version transaction takes a fresh
READ COMMITTED snapshot, so it can only ever reference already-committed
batches, and the M2M bindings are written atomically together with the version.
"""

from __future__ import annotations

import statistics
from decimal import Decimal

from django.db import transaction
from django.db.models import Max
from django.utils.timezone import now

from pretalx.event.domain.queries.team import user_reviewer_teams_in_event
from pretalx.submission.models import (
    ExposureKind,
    ExposureSource,
    Review,
    ReviewExposureBatch,
    ReviewExposureEntry,
    ReviewScoreVersion,
)

LEDGER_SETTING = "review_exposure_ledger"
STARTED_SETTING = "review_exposure_ledger_started"

IDENTITY_NAME = "name"
IDENTITY_EMAIL = "email"
IDENTITY_PROFILE_CODE = "profile_code"
IDENTITY_PROFILE_LINK = "profile_link"
IDENTITY_BIOGRAPHY = "biography"

ATTRIBUTION_NAME = "name"
ATTRIBUTION_CODE = "code"


def ledger_enabled(event) -> bool:
    return bool(event.review_settings.get(LEDGER_SETTING, False))


def observation_start(event):
    """Immutable observation start point, or ``None`` (unknown) if the
    ledger has never been enabled for this event."""
    return event.review_settings.get(STARTED_SETTING)


def ensure_observation_start(event, save=True):
    """Stamp the immutable observation start point on first enable.

    Never resets an existing timestamp, i.e. disable/enable cycles keep the
    first enablement as the start point.
    """
    settings = dict(event.review_settings or {})
    if not settings.get(STARTED_SETTING):
        settings[STARTED_SETTING] = now().isoformat()
        event.review_settings = settings
        if save:
            event.save(update_fields=["review_settings"])
    return event.review_settings[STARTED_SETTING]


def is_reviewer_viewer(event, user) -> bool:
    if not event or not user or not user.is_authenticated:
        return False
    return bool(user_reviewer_teams_in_event(user, event))


def _round_one(value):
    if value is None:
        return None
    return value.quantize(Decimal("0.1"))


def build_display_score_snapshot(*, event, submission, user, own_prior_score=None):
    """Aggregate scores formed by OTHER reviewers' non-null scores, as
    displayed before this submission was (re-)written by the viewer."""
    other_scores = list(
        Review.objects.filter(submission=submission)
        .exclude(user=user)
        .exclude(score__isnull=True)
        .values_list("score", flat=True)
    )
    count = len(other_scores)
    median = _round_one(statistics.median(other_scores)) if count else None
    mean = _round_one(sum(other_scores) / count) if count else None
    independent = _independent_snapshot(event, submission, user)

    return {
        "aggregate_method": event.review_settings.get("aggregate_method", "median"),
        "others_nonnull_count": count,
        "median": str(median) if median is not None else None,
        "mean": str(mean) if mean is not None else None,
        "independent": independent,
        "own_prior_score": (
            str(own_prior_score) if own_prior_score is not None else None
        ),
    }


def _independent_snapshot(event, submission, user):
    from pretalx.submission.models import ReviewScore  # noqa: PLC0415 -- local to avoid cycle

    result = {}
    other_reviews = Review.objects.filter(submission=submission).exclude(user=user)
    for category in event.score_categories.filter(is_independent=True, active=True):
        values = list(
            ReviewScore.objects.filter(
                category=category, reviews__in=other_reviews
            ).values_list("value", flat=True)
        )
        if not values:
            continue
        result[str(category.pk)] = {
            "count": len(values),
            "median": str(_round_one(statistics.median(values))),
            "mean": str(_round_one(sum(values) / len(values))),
        }
    return result


def _score_choices_snapshot(review, submission):
    """Immutable copy of the per-category selections of one review."""
    selected = {score.category_id: score for score in review.scores.select_related("category")}
    result = []
    for category in submission.score_categories:
        score = selected.get(category.pk)
        result.append(
            {
                "category_id": category.pk,
                "category_name": str(category.name),
                "score_id": score.pk if score else None,
                "value": str(score.value) if score else None,
                "label": score.label if score else None,
            }
        )
    return result


class RequestLedger:
    """In-request collector for exposure entries.

    Views and serializers call the ``add_*`` methods while building the actual
    output; nothing is persisted until :meth:`commit` runs (after the response
    has been successfully rendered). Entries are merged per
    ``(submission, kind)`` so every category yields at most one entry per
    proposal per batch.
    """

    def __init__(self, *, event, viewer, source, request=None, page=""):
        self.event = event
        self.viewer = viewer
        self.source = source
        self.page = str(page) if page else ""
        self.path = request.path if request is not None else ""
        self.request_method = request.method if request is not None else ""
        # Check the cheap setting flag first so disabled ledgers never trigger
        # permission/team queries (read paths stay at zero added queries).
        self.active = ledger_enabled(event) and is_reviewer_viewer(event, viewer)
        # key: (submission_id, kind) -> merged draft dict
        self._drafts = {}

    def __bool__(self):
        return bool(self._drafts)

    def _draft(self, submission, kind, position):
        key = (submission.pk, kind)
        draft = self._drafts.get(key)
        if draft is None:
            draft = {
                "event_id": self.event.pk,
                "submission": submission,
                "submission_code": submission.code,
                "kind": kind,
                "position": position,
                "details": {},
            }
            self._drafts[key] = draft
        else:
            draft["position"] = min(draft["position"], position)
        return draft

    def add_identity(self, submission, *, forms, position=0):
        if not self.active:
            return
        if not forms:
            return
        draft = self._draft(submission, ExposureKind.AUTHOR_IDENTITY, position)
        existing = set(draft["details"].get("forms", []))
        existing.update(forms)
        draft["details"]["forms"] = sorted(existing)

    def add_other_review(
        self,
        submission,
        *,
        review_id,
        review_user_id=None,
        has_text=False,
        has_score=False,
        has_answers=False,
        attribution=None,
        position=0,
    ):
        if not self.active:
            return
        # The viewer's own review is never an exposure of "other reviewers'"
        # reviews or scores, even when callers hand in the full row list.
        if review_user_id is not None and review_user_id == self.viewer.pk:
            return
        if not (has_text or has_score or has_answers):
            return
        draft = self._draft(submission, ExposureKind.OTHER_REVIEW, position)
        reviews = draft["details"].setdefault("reviews", [])
        if any(item["review_id"] == review_id for item in reviews):
            return
        reviews.append(
            {
                "review_id": review_id,
                "has_text": bool(has_text),
                "has_score": bool(has_score),
                "has_answers": bool(has_answers),
                "attribution": attribution,
                "position": len(reviews),
            }
        )

    def add_aggregate(
        self,
        submission,
        *,
        methods=None,
        others_nonnull_count,
        independent=None,
        position=0,
    ):
        if not self.active:
            return
        if not others_nonnull_count:
            return
        draft = self._draft(submission, ExposureKind.AGGREGATE_SCORE, position)
        details = draft["details"]
        details["others_nonnull_count"] = max(
            others_nonnull_count, details.get("others_nonnull_count", 0)
        )
        if methods:
            merged = details.setdefault("methods", {})
            for key, value in methods.items():
                if value is not None:
                    merged[key] = value
        if independent:
            merged_independent = details.setdefault("independent", {})
            merged_independent.update(independent)

    def commit(self, *, status_code):
        """Persist batch and all entries in one transaction.

        Returns ``None`` when inactive or empty, so empty/in-scope responses
        leave no "empty batch" behind and never open a transaction. Any
        failure rolls the whole batch back (no half batches).
        """
        if not self.active or not self._drafts:
            return None
        return self._persist(status_code)

    @transaction.atomic
    def _persist(self, status_code):
        batch = ReviewExposureBatch.objects.create(
            event_id=self.event.pk,
            viewer=self.viewer,
            viewer_code=getattr(self.viewer, "code", None),
            source=self.source,
            path=self.path[:512],
            request_method=self.request_method[:8],
            status_code=status_code,
            page=self.page[:32],
        )
        drafts = sorted(self._drafts.values(), key=lambda d: (d["position"], d["kind"]))
        ReviewExposureEntry.objects.bulk_create(
            ReviewExposureEntry(
                batch=batch,
                event_id=draft["event_id"],
                submission=draft["submission"],
                submission_code=draft["submission_code"],
                position=draft["position"],
                kind=draft["kind"],
                details=draft["details"],
            )
            for draft in drafts
        )
        return batch


def get_request_ledger(request, *, source, page=""):
    """Attach (or fetch) the request-scoped ledger.

    Always returns a ledger object so call sites can call ``add_*``
    unconditionally; the ledger is simply inactive when the viewer is not a
    reviewer or the ledger is disabled.
    """
    existing = getattr(request, "_review_exposure_ledger", None)
    if existing is not None:
        return existing
    event = getattr(request, "event", None)
    user = getattr(request, "user", None)
    if event is None or not getattr(user, "is_authenticated", False):
        ledger = None
    else:
        ledger = RequestLedger(
            event=event, viewer=user, source=source, request=request, page=page
        )
    request._review_exposure_ledger = ledger
    return ledger


def create_score_version(
    *, review, source, own_prior_score=None, event=None, submission=None, user=None
):
    """Create the immutable version for one accepted human review submission.

    Runs inside the caller's existing write transaction. Locks the review row
    to serialise per-review version numbers, then — with a fresh READ
    COMMITTED snapshot — binds only exposure batches already committed for the
    same reviewer and proposal.
    """
    event = event or review.event
    if not ledger_enabled(event):
        return None
    submission = submission or review.submission

    with transaction.atomic():
        # Row lock serialises concurrent version creation for the same review.
        # select_related("user") makes the author explicit rather than a lazy
        # fetch on a FETCH_RAISE instance.
        locked = (
            Review.objects.select_for_update()
            .select_related("user")
            .get(pk=review.pk)
        )
        user = user or locked.user
        # Evaluated after acquiring the lock: under READ COMMITTED this
        # statement sees exactly the batches committed at this point, and can
        # never see uncommitted or rolled-back batches.
        bound_batch_ids = list(
            ReviewExposureBatch.objects.filter(
                event_id=event.pk,
                viewer_id=user.pk,
                entries__submission_id=submission.pk,
            )
            .values_list("id", flat=True)
            .distinct()
        )
        current_max = (
            ReviewScoreVersion.objects.filter(review=locked)
            .aggregate(maximum=Max("version_number"))
            .get("maximum")
        )
        version = ReviewScoreVersion.objects.create(
            event=event,
            review=locked,
            submission=submission,
            submission_code=submission.code,
            user=user,
            user_code=getattr(user, "code", None),
            version_number=(current_max or 0) + 1,
            source=source,
            text_snapshot=locked.text,
            score_choices_snapshot=_score_choices_snapshot(locked, submission),
            total_score=locked.score,
            display_score_snapshot=build_display_score_snapshot(
                event=event,
                submission=submission,
                user=user,
                own_prior_score=own_prior_score,
            ),
        )
        version.bound_batches.add(*bound_batch_ids)
    return version


# ---------------------------------------------------------------------------
# Thin UI collector helpers. They receive exactly the objects the templates
# use, so registration stays consistent with what is actually rendered.
# ---------------------------------------------------------------------------


def record_single_page_exposure(
    ledger,
    *,
    submission,
    speakers,
    can_view_speakers,
    review_rows,
    can_view_reviewer_names,
):
    """Collect exposures for the single-submission review page.

    ``speakers`` and ``review_rows`` are the view context structures the
    template iterates (already gated by the same permissions the template
    uses).
    """
    if not ledger or not ledger.active:
        return
    if can_view_speakers and speakers:
        forms = {IDENTITY_NAME}
        if any(speaker.get("other_submissions") for speaker in speakers):
            forms.add(IDENTITY_PROFILE_LINK)
        if submission.event.cfp.request_biography and any(
            getattr(speaker.get("speaker"), "biography", None) for speaker in speakers
        ):
            forms.add(IDENTITY_BIOGRAPHY)
        ledger.add_identity(submission, forms=forms)
    attribution = ATTRIBUTION_NAME if can_view_reviewer_names else None
    for index, row in enumerate(review_rows or []):
        row_user = row.get("user")
        ledger.add_other_review(
            submission,
            review_id=row["id"],
            review_user_id=getattr(row_user, "pk", None),
            has_text=bool(row.get("text")),
            has_score=row.get("score") != "×",
            has_answers=any(answer is not None for answer in row.get("answers", [])),
            attribution=attribution,
            position=index,
        )


def record_bulk_rows_exposure(ledger, submissions, *, can_view_speakers):
    """Collect author identity rows for the bulk review page/row fragment."""
    if not ledger or not ledger.active or not can_view_speakers:
        return
    for position, submission in enumerate(submissions):
        if submission.sorted_speakers:
            ledger.add_identity(
                submission, forms=[IDENTITY_NAME], position=position
            )


def classify_speaker_output(speakers):
    """Identity forms actually present in a serialized ``speakers`` field.

    ``speakers`` is either a list of cross-proposal SpeakerProfile codes (the
    default API output, which already allows linking a concrete speaker across
    proposals) or a list of expanded speaker objects.
    """
    forms = set()
    for item in speakers or []:
        if isinstance(item, str):
            forms.add(IDENTITY_PROFILE_CODE)
        elif isinstance(item, dict):
            if item.get("code"):
                forms.add(IDENTITY_PROFILE_CODE)
            if item.get("name"):
                forms.add(IDENTITY_NAME)
            if item.get("email"):
                forms.add(IDENTITY_EMAIL)
            if item.get("biography"):
                forms.add(IDENTITY_BIOGRAPHY)
    return sorted(forms)


def build_ledger_export(event):
    """Minimal, material-free evidence for organisers (see FR-18/FR-19).

    Contains only codes, categories, sources, order and the version snapshots
    required by the feature; never proposal materials such as abstracts,
    descriptions, notes or biographies.
    """
    settings = event.review_settings or {}
    enabled = bool(settings.get(LEDGER_SETTING, False))
    start = settings.get(STARTED_SETTING)

    batches = []
    entries_by_batch = {}
    batch_qs = (
        ReviewExposureBatch.objects.filter(event=event)
        .prefetch_related("entries")
        .order_by("id")
    )
    for batch in batch_qs:
        batches.append(
            {
                "batch_id": batch.pk,
                "source": batch.source,
                "created": batch.created.isoformat() if batch.created else None,
                "request_method": batch.request_method,
                "status_code": batch.status_code,
                "page": batch.page,
                "viewer_code": batch.viewer_code,
            }
        )
        entries_by_batch[batch.pk] = [
            {
                "submission_code": entry.submission_code,
                "kind": entry.kind,
                "position": entry.position,
                "details": entry.details,
            }
            for entry in sorted(batch.entries.all(), key=lambda e: (e.position, e.kind))
        ]

    versions = []
    version_qs = (
        ReviewScoreVersion.objects.filter(event=event)
        .select_related("user")
        .order_by("id")
    )
    for version in version_qs:
        bound = [
            {
                "batch_id": batch.pk,
                "source": batch.source,
                "created": batch.created.isoformat() if batch.created else None,
                "page": batch.page,
                "kinds": sorted(
                    {entry.kind for entry in entries_by_batch.get(batch.pk, [])}
                ),
                "entries": entries_by_batch.get(batch.pk, []),
            }
            for batch in version.bound_batches.order_by("id")
        ]
        versions.append(
            {
                "version_id": version.pk,
                "version_number": version.version_number,
                "created": version.created.isoformat() if version.created else None,
                "source": version.source,
                "user_code": version.user_code,
                "user_name": version.user.name if version.user else None,
                "submission_code": version.submission_code,
                "text_snapshot": version.text_snapshot,
                "score_choices_snapshot": version.score_choices_snapshot,
                "total_score": (
                    str(version.total_score)
                    if version.total_score is not None
                    else None
                ),
                "display_score_snapshot": version.display_score_snapshot,
                # Explicit empty list = in scope but no exposure recorded
                # before this version; distinguishable from "unknown" below.
                "bound_exposures": bound,
            }
        )

    return {
        "meta": {
            "event": event.slug,
            "enabled": enabled,
            # None means: the ledger has never been enabled, all history is
            # unknown rather than "not recorded".
            "observation_start": start,
            "history_status": "recording" if enabled else (
                "unknown" if start is None else "disabled"
            ),
            "generated_at": now().isoformat(),
        },
        "batches": batches,
        "versions": versions,
    }


