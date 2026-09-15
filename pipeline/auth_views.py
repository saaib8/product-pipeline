"""Session auth for the Next.js reviewer app.

Session cookies rather than tokens: the frontend proxies ``/api/*`` to Django, so the
browser treats both as same-origin and the cookie needs no special handling. It also
means a reviewer's identity on a `ReviewEvent` is a real Django user with no extra
plumbing.
"""

from __future__ import annotations

from django.contrib.auth import authenticate, login, logout
from django.middleware.csrf import get_token
from rest_framework import status as http
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response


def _user_payload(user) -> dict:
    return {"id": user.id, "username": user.get_username(), "is_staff": user.is_staff}


@api_view(["GET"])
@permission_classes([AllowAny])
def csrf(request):
    """Hand the frontend a CSRF token (and set the cookie) before it POSTs."""
    return Response({"csrfToken": get_token(request)})


@api_view(["POST"])
@permission_classes([AllowAny])
def login_view(request):
    user = authenticate(
        request,
        username=request.data.get("username"),
        password=request.data.get("password"),
    )
    if user is None:
        return Response({"detail": "Incorrect username or password."},
                        status=http.HTTP_401_UNAUTHORIZED)
    login(request, user)
    return Response(_user_payload(user))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def logout_view(request):
    logout(request)
    return Response(status=http.HTTP_204_NO_CONTENT)


@api_view(["GET"])
@permission_classes([AllowAny])
def me(request):
    """Who am I? The frontend calls this on load to decide login vs queue."""
    if not request.user.is_authenticated:
        return Response({"detail": "Not authenticated."}, status=http.HTTP_401_UNAUTHORIZED)
    return Response(_user_payload(request.user))
