"""DRF object-level permission for PaymentIntent ownership."""
from __future__ import annotations

from rest_framework.permissions import BasePermission

from .services.payment_service import get_intent_for_user


class IsIntentOwner(BasePermission):
    """Check that the authenticated user owns the requested intent.

    The intent's appointment must link back to the requester's `doc_patients`
    row via `user_id`. We resolve that in `get_intent_for_user` to keep the
    permission a single SQL round-trip.
    """

    message = "You do not own this payment intent."

    def has_permission(self, request, view) -> bool:
        intent_id = view.kwargs.get("intent_id")
        if not intent_id:
            return False
        user = getattr(request, "user", None)
        if not user or not getattr(user, "is_authenticated", False):
            return False
        # Cache the lookup on the request so the view can reuse it
        # without a second query.
        intent = get_intent_for_user(intent_id=intent_id, requester_user_id=user.id)
        if intent is None:
            return False
        request._payment_intent_record = intent  # type: ignore[attr-defined]
        return True
