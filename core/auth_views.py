"""Django session endpoints for local PostgreSQL accounts."""
from django.contrib.auth import authenticate, login, logout
from django.middleware.csrf import get_token
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from rest_framework import status
from rest_framework.authentication import SessionAuthentication
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView


@method_decorator(ensure_csrf_cookie, name="dispatch")
class CsrfView(APIView):
    """Set Django's CSRF cookie before a browser submits credentials."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        # Return the masked token as JSON as well as setting the CSRF cookie.
        # This works when the frontend is aidoccall.com and the API is hosted
        # at api.aidoccall.com, where JavaScript cannot read an API host-only
        # cookie directly.
        return Response({"success": True, "csrfToken": get_token(request)})


@method_decorator(csrf_protect, name="dispatch")
class LoginView(APIView):
    """
    POST /api/auth/login/ - Login with Django session authentication.

    Request body:
    {
        "email": "user@example.com",
        "password": "password123"
    }
    """
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        """Login user and create session."""
        email = str(request.data.get("email") or "").strip()
        password = request.data.get("password")

        if not email or not password:
            return Response({
                'success': False,
                'message': 'Email and password are required'
            }, status=status.HTTP_400_BAD_REQUEST)

        # The custom user model authenticates by email.  Passwords are passed
        # only to Django's authentication backends and are never logged.
        user = authenticate(request, username=email, password=password)

        if user is not None:
            login(request, user)
            return Response({
                'success': True,
                'message': 'Login successful',
                "data": {
                    "id": user.id,
                    "email": user.email,
                    "full_name": user.full_name,
                    "role": user.role,
                }
            })

        return Response({
            'success': False,
            'message': 'Invalid email or password'
        }, status=status.HTTP_401_UNAUTHORIZED)


class LogoutView(APIView):
    """End the current Django browser session."""

    authentication_classes = [SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        logout(request)
        return Response({"success": True, "message": "Logged out"})
