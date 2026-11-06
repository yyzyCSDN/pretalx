# SPDX-FileCopyrightText: 2026-present Tobias Kunze
# SPDX-License-Identifier: AGPL-3.0-only WITH LicenseRef-Pretalx-AGPL-3.0-Terms

"""Immutable audit ledger recording which review-relevant information the
system actually displayed to reviewers, and immutable per-submission versions
of accepted human review submissions.

These models deliberately hold no foreign keys to ``ReviewPhase``, ``Track`` or
``Team`` objects: phase, track and team changes must never be able to clear or
rewrite ledger history. ``user`` and ``submission`` use ``SET_NULL`` and are
additionally stored as plain codes, so deleting users or proposals cannot erase
the recorded evidence.
"""

from django.db import models
from django.utils.translation import gettext_lazy as _

from pretalx.common.models.mixins import PretalxModel


class ExposureSource(models.TextChoices):
    UI_SINGLE = "ui_single", _("Single review page")
    UI_BULK = "ui_bulk", _("Bulk review page")
    UI_DASHBOARD = "ui_dashboard", _("Review dashboard")
    API_REVIEW_LIST = "api_review_list", _("API review list")
    API_REVIEW_DETAIL = "api_review_detail", _("API review detail")
    API_SUBMISSION_LIST = "api_submission_list", _("API submission list")
    API_SUBMISSION_DETAIL = "api_submission_detail", _("API submission detail")


class ExposureKind(models.TextChoices):
    AUTHOR_IDENTITY = "author_identity", _("Author identity")
    OTHER_REVIEW = "other_review", _("Other reviewers' review or score")
    AGGREGATE_SCORE = "aggregate_score", _("Aggregate score containing other scores")


class ReviewExposureBatch(PretalxModel):
    """One actually delivered 2xx response to a logged-in reviewer, and the
    set of information exposures contained in that response."""

    event = models.ForeignKey(
        to="event.Event", related_name="review_exposure_batches", on_delete=models.CASCADE
    )
    viewer = models.ForeignKey(
        to="person.User",
        related_name="review_exposure_batches",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    viewer_code = models.CharField(max_length=16, null=True, blank=True)
    source = models.CharField(max_length=32, choices=ExposureSource.choices)
    path = models.CharField(max_length=512, default="", blank=True)
    request_method = models.CharField(max_length=8, default="", blank=True)
    status_code = models.PositiveSmallIntegerField()
    # Pagination identifier of the page actually rendered (if any); materials on
    # other pages are never part of this batch.
    page = models.CharField(max_length=32, default="", blank=True)

    class Meta:
        verbose_name_plural = _("Review exposure batches")
        ordering = ("id",)
        indexes = [
            models.Index(fields=["event", "viewer", "id"]),
        ]

    def __str__(self):
        return f"ReviewExposureBatch(event={self.event_id}, viewer={self.viewer_code}, source={self.source})"


class ReviewExposureEntry(PretalxModel):
    """One exposure category for one proposal within an exposure batch.

    ``details`` stores minimal audit flags only, never proposal material:

    * ``author_identity``: ``{"forms": ["name", "email", "profile_code",
      "profile_link", "biography"], ...}``
    * ``other_review``: ``{"review_id": int, "has_text": bool, "has_score":
      bool, "has_answers": bool, "reviewer_attribution": "name"|"code"|None}``
    * ``aggregate_score``: ``{"methods": {"median": x, "mean": y},
      "others_nonnull_count": n, "independent": {category_id: value}}``
    """

    batch = models.ForeignKey(
        to=ReviewExposureBatch, related_name="entries", on_delete=models.CASCADE
    )
    event = models.ForeignKey(
        to="event.Event",
        related_name="review_exposure_entries",
        on_delete=models.CASCADE,
    )
    submission = models.ForeignKey(
        to="submission.Submission",
        related_name="review_exposure_entries",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    submission_code = models.CharField(max_length=16)
    position = models.PositiveIntegerField(default=0)
    kind = models.CharField(max_length=24, choices=ExposureKind.choices)
    details = models.JSONField(default=dict)

    class Meta:
        verbose_name_plural = _("Review exposure entries")
        ordering = ("batch_id", "position", "id")
        unique_together = (("batch", "submission", "kind"),)
        indexes = [
            models.Index(fields=["event", "kind"]),
            models.Index(fields=["submission_code"]),
        ]

    def __str__(self):
        return f"ReviewExposureEntry(batch={self.batch_id}, submission={self.submission_code}, kind={self.kind})"


class ReviewScoreVersion(PretalxModel):
    """An immutable snapshot of one accepted human review submission.

    Bound exposure batches are exactly the batches for the same reviewer and
    proposal that were committed to the database before this version's own
    transaction committed.
    """

    event = models.ForeignKey(
        to="event.Event", related_name="review_score_versions", on_delete=models.CASCADE
    )
    review = models.ForeignKey(
        to="submission.Review",
        related_name="score_versions",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    submission = models.ForeignKey(
        to="submission.Submission",
        related_name="review_score_versions",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    submission_code = models.CharField(max_length=16)
    user = models.ForeignKey(
        to="person.User",
        related_name="review_score_versions",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    user_code = models.CharField(max_length=16, null=True, blank=True)
    version_number = models.PositiveIntegerField()
    source = models.CharField(max_length=32, choices=ExposureSource.choices)
    text_snapshot = models.TextField(null=True, blank=True)
    score_choices_snapshot = models.JSONField(default=list)
    total_score = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    display_score_snapshot = models.JSONField(default=dict)
    bound_batches = models.ManyToManyField(
        to=ReviewExposureBatch, related_name="score_versions", blank=True
    )

    class Meta:
        verbose_name_plural = _("Review score versions")
        ordering = ("id",)
        unique_together = (("review", "version_number"),)
        indexes = [
            models.Index(fields=["event", "user"]),
            models.Index(fields=["submission_code"]),
        ]

    def __str__(self):
        return (
            f"ReviewScoreVersion(event={self.event_id}, submission={self.submission_code}, "
            f"user={self.user_code}, number={self.version_number})"
        )
