"""Password policy shared by cookie-session entry points and GraphQL gates."""
from django.utils import timezone

from core.gql_errors import AuthenticationRequired


def password_expired(user):
    expires_at = getattr(getattr(user, 'i_user', None), 'password_validity', None)
    if expires_at is None:
        return False
    current_time = timezone.now()
    if timezone.is_naive(expires_at) and timezone.is_aware(current_time):
        expires_at = timezone.make_aware(expires_at)
    elif timezone.is_aware(expires_at) and timezone.is_naive(current_time):
        expires_at = timezone.make_naive(expires_at)
    return expires_at <= current_time


def require_active_password(user):
    if password_expired(user):
        raise AuthenticationRequired('PASSWORD_EXPIRED')
