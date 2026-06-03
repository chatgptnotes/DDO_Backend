"""DRF permissions for the payments app: PaymentIntent ownership and
clinic-scoped access to Stripe Connect onboarding."""
from __future__ import annotations

from rest_framework.permissions import BasePermission

from core.roles import clinic_scope_id, has_role

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


class IsClinicScopedAdmin(BasePermission):
    """Gate the Stripe Connect onboarding endpoints.

    `superadmin` is allowed unconditionally (and may pass an explicit
    `clinic_id` to act on any clinic). A `clinical_admin` is allowed only for
    the clinic they are scoped to via `user_roles.scope_id` — when the request
    names a `clinic_id`, it must match their scope; when it omits one, the
    clinic is derived from their scope downstream, so role membership alone is
    sufficient here.

    Any other role is rejected. The clinic actually used is always re-resolved
    server-side in the service layer — this permission is the first gate, not
    the only one.
    """

    message = "You may only manage your own clinic's payment settings."

    def has_permission(self, request, view) -> bool:
        user = getattr(request, "user", None)
        if not user or not getattr(user, "is_authenticated", False):
            return False
        if has_role(user.id, "superadmin"):
            return True
        if not has_role(user.id, "clinical_admin"):
            return False

        requested = (
            (getattr(request, "data", None) or {}).get("clinic_id")
            or request.query_params.get("clinic_id")
            or view.kwargs.get("clinic_id")
        )
        if requested is None:
            # No clinic named — it will be derived from the caller's scope.
            return True
        return str(requested) == str(clinic_scope_id(user.id) or "")
