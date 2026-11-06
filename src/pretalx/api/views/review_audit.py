# SPDX-FileCopyrightText: 2026-present Tobias Kunze
# SPDX-License-Identifier: AGPL-3.0-only WITH LicenseRef-Pretalx-AGPL-3.0-Terms

"""ViewSet mixins registering review information exposure from actual API
2xx output.

Classification inspects the already-serialized ``response.data`` (so omitted
fields and pagination-excluded objects are never registered), together with
minimal database lookups needed to distinguish the viewer's own reviews and
aggregates that contain other reviewers' non-null scores.
"""

from django.db.models import Count

from pretalx.submission.domain.review_audit import (
    ATTRIBUTION_CODE,
    ATTRIBUTION_NAME,
    ExposureSource,
    classify_speaker_output,
    get_request_ledger,
)
from pretalx.submission.models import Review


def _serialized_rows(data):
    if isinstance(data, dict) and "results" in data:
        return list(data["results"] or [])
    if isinstance(data, dict):
        return [data]
    return []


class ReviewApiExposureAuditMixin:
    """Audits the reviews endpoint (list, retrieve and write responses)."""

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        self._audit_review_exposure(request, response)
        return response

    def _audit_review_exposure(self, request, response):
        if not 200 <= response.status_code < 300:
            return
        source = (
            ExposureSource.API_REVIEW_LIST
            if self.action == "list"
            else ExposureSource.API_REVIEW_DETAIL
        )
        ledger = get_request_ledger(
            request,
            source=source,
            page=request.query_params.get("page", ""),
        )
        if not ledger or not ledger.active:
            return
        rows = _serialized_rows(response.data)
        if not rows:
            return
        review_ids = [row["id"] for row in rows if row.get("id") is not None]
        ownership = dict(
            Review.objects.filter(pk__in=review_ids).values_list("id", "user_id")
        )
        submission_codes = set()
        for row in rows:
            submission = row.get("submission")
            if isinstance(submission, str):
                submission_codes.add(submission)
            elif isinstance(submission, dict) and submission.get("code"):
                submission_codes.add(submission["code"])
        submissions = {
            submission.code: submission
            for submission in self.event.submissions.filter(
                code__in=submission_codes
            )
        }
        viewer_id = request.user.pk

        for position, row in enumerate(rows):
            submission_field = row.get("submission")
            code = (
                submission_field
                if isinstance(submission_field, str)
                else (submission_field or {}).get("code")
            )
            submission = submissions.get(code)
            if submission is None:
                continue
            # An expanded submission may itself reveal author identity, also
            # in write responses (registered after the version commits).
            if isinstance(submission_field, dict):
                forms = classify_speaker_output(submission_field.get("speakers"))
                if forms:
                    ledger.add_identity(
                        submission, forms=forms, position=position
                    )
            # The viewer's own review (including the just-written one) is not
            # "somebody else's review".
            if ownership.get(row.get("id")) == viewer_id:
                continue
            attribution = None
            if "user" in row and row.get("user"):
                user_field = row["user"]
                if isinstance(user_field, dict):
                    attribution = (
                        ATTRIBUTION_NAME
                        if (user_field.get("name") or user_field.get("email"))
                        else ATTRIBUTION_CODE
                    )
                else:
                    attribution = ATTRIBUTION_CODE
            ledger.add_other_review(
                submission,
                review_id=row["id"],
                has_text=bool(row.get("text")),
                has_score=row.get("score") is not None,
                has_answers=bool(row.get("answers")),
                attribution=attribution,
                position=position,
            )


class SubmissionApiExposureAuditMixin:
    """Audits the submissions endpoint (list/retrieve)."""

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        self._audit_submission_exposure(request, response)
        return response

    def _audit_submission_exposure(self, request, response):
        if not 200 <= response.status_code < 300 or self.action not in (
            "list",
            "retrieve",
        ):
            return
        source = (
            ExposureSource.API_SUBMISSION_LIST
            if self.action == "list"
            else ExposureSource.API_SUBMISSION_DETAIL
        )
        ledger = get_request_ledger(
            request,
            source=source,
            page=request.query_params.get("page", ""),
        )
        if not ledger or not ledger.active:
            return
        rows = _serialized_rows(response.data)
        if not rows:
            return
        codes = [row["code"] for row in rows if row.get("code")]
        submissions = {
            submission.code: submission
            for submission in self.get_queryset().filter(code__in=codes)
        }
        others_counts = {
            row["submission__code"]: row["count"]
            for row in (
                Review.objects.filter(
                    submission__event=self.event,
                    submission__code__in=codes,
                )
                .exclude(user=request.user)
                .exclude(score__isnull=True)
                .values("submission__code")
                .annotate(count=Count("id"))
            )
        }
        for position, row in enumerate(rows):
            submission = submissions.get(row.get("code"))
            if submission is None:
                continue
            forms = classify_speaker_output(row.get("speakers"))
            if forms:
                ledger.add_identity(submission, forms=forms, position=position)
            # Aggregate keys are only present when the serializer included
            # them (organiser serializer, not trimmed by omit), and count as
            # exposure only if others' non-null scores feed the aggregate.
            methods = {}
            for key, label in (("median_score", "median"), ("mean_score", "mean")):
                if key in row and row.get(key) is not None:
                    methods[label] = row[key]
            others_nonnull = others_counts.get(row["code"], 0)
            if methods and others_nonnull:
                ledger.add_aggregate(
                    submission,
                    methods=methods,
                    others_nonnull_count=others_nonnull,
                    position=position,
                )
