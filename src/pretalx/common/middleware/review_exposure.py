# SPDX-FileCopyrightText: 2026-present Tobias Kunze
# SPDX-License-Identifier: AGPL-3.0-only WITH LicenseRef-Pretalx-AGPL-3.0-Terms

import logging

logger = logging.getLogger(__name__)

REQUEST_ATTR = "_review_exposure_ledger"


class ReviewExposureLedgerMiddleware:
    """Persist the per-request review exposure ledger after successful output.

    Views and API viewsets collect exposure drafts on ``request`` while they
    build the actual response. The drafts are committed here, after the view
    transaction has committed and the response has been rendered, and only for
    2xx responses. As the middleware is ordered behind ``EventMiddleware`` it
    executes inside the event scope.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        ledger = getattr(request, REQUEST_ATTR, None)
        import sys

        print(
            f"DEBUG-MW path={request.path} status={response.status_code} "
            f"ledger={ledger is not None} active={getattr(ledger, 'active', None)} "
            f"drafts={len(getattr(ledger, '_drafts', {}))}",
            file=sys.stderr,
        )
        if (
            ledger is not None
            and ledger.active
            and ledger
            and 200 <= response.status_code < 300
        ):
            try:
                ledger.commit(status_code=response.status_code)
            except Exception:  # pragma: no cover -- defensive, audit must never break delivery
                logger.exception(
                    "Failed to commit review exposure ledger for %s", request.path
                )
        return response
