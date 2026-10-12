"""Exercise real cookies and logout with production CSRF checks enabled."""
import datetime
import json

from django.conf import settings
from django.contrib.auth import SESSION_KEY
from django.test import Client, TestCase, override_settings
from django.utils import timezone
from graphql_jwt.settings import jwt_settings
from graphql_jwt.shortcuts import get_token

from core.test_helpers import create_test_interactive_user


# CI loads dev settings, which remove this middleware at import time. Restore
# the production CSRF pipeline, not only the MODE flag, for these requests.
PRODUCTION_MIDDLEWARE = list(settings.MIDDLEWARE)
if 'django.middleware.csrf.CsrfViewMiddleware' not in PRODUCTION_MIDDLEWARE:
    PRODUCTION_MIDDLEWARE.insert(
        PRODUCTION_MIDDLEWARE.index('django.contrib.sessions.middleware.SessionMiddleware') + 1,
        'django.middleware.csrf.CsrfViewMiddleware',
    )


@override_settings(
    MIDDLEWARE=PRODUCTION_MIDDLEWARE,
    MODE='prod', IS_TESTING=False, CSRF_USE_SESSIONS=True,
    USER_AGENT_CSRF_BYPASS=[], ALLOWED_HOSTS=['testserver'],
)
class SessionAuthenticationTests(TestCase):
    def setUp(self):
        self.user = create_test_interactive_user(username='session_review')
        self.client = Client(enforce_csrf_checks=True)
        self.root = f'/{settings.SITE_ROOT()}'
        self.current_user_url = f'{self.root}core/users/current_user/'
        self.logout_url = f'{self.root}core/logout/'

    def login_session(self):
        # Equivalent to the session created by Django admin authentication.
        self.client.force_login(self.user, backend='django.contrib.auth.backends.ModelBackend')

    def query(self, query):
        return self.client.post(
            f'{self.root}graphql', json.dumps({'query': query}),
            content_type='application/json',
        )

    def csrf_token(self):
        response = self.query('mutation { getCsrfToken { csrfToken } }')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertNotIn('errors', data)
        return data['data']['getCsrfToken']['csrfToken']

    def expire_password(self):
        self.user.i_user.password_validity = timezone.now() - datetime.timedelta(days=1)
        self.user.i_user.save()

    def test_django_session_without_jwt_loads_current_user(self):
        self.login_session()
        self.assertNotIn(jwt_settings.JWT_COOKIE_NAME, self.client.cookies)
        response = self.client.get(self.current_user_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['username'], self.user.username)
        self.assertEqual(response.json()['authMode'], 'session')

    def test_admin_session_cannot_bypass_expired_password(self):
        self.login_session()
        self.expire_password()
        response = self.client.get(self.current_user_url)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['detail'], 'PASSWORD_EXPIRED')
        response = self.query('{ languages { name } }')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['errors'][0]['message'], 'PASSWORD_EXPIRED')

    def test_logout_flushes_admin_session_and_reload_is_anonymous(self):
        self.login_session()
        token = self.csrf_token()
        self.client.cookies[jwt_settings.JWT_COOKIE_NAME] = 'expired-or-invalid'
        self.client.cookies[jwt_settings.JWT_REFRESH_TOKEN_COOKIE_NAME] = 'stale-refresh'
        response = self.client.post(self.logout_url, HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 204)
        self.assertNotIn(SESSION_KEY, self.client.session)
        self.assertEqual(response.cookies[jwt_settings.JWT_COOKIE_NAME]['max-age'], 0)
        self.assertEqual(response.cookies[jwt_settings.JWT_REFRESH_TOKEN_COOKIE_NAME]['max-age'], 0)
        self.assertEqual(self.client.get(self.current_user_url).status_code, 401)

    def test_logout_requires_post_and_valid_csrf(self):
        self.login_session()
        self.assertEqual(self.client.get(self.logout_url).status_code, 405)
        self.assertEqual(self.client.post(self.logout_url).status_code, 403)
        self.assertIn(SESSION_KEY, self.client.session)

    def test_expired_password_can_still_logout_safely(self):
        self.login_session()
        self.expire_password()
        token = self.csrf_token()
        self.assertEqual(self.client.post(self.logout_url, HTTP_X_CSRFTOKEN=token).status_code, 204)
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_single_jwt_can_be_renewed_without_refresh_cookie(self):
        self.client.cookies[jwt_settings.JWT_COOKIE_NAME] = get_token(self.user)
        self.assertNotIn(jwt_settings.JWT_REFRESH_TOKEN_COOKIE_NAME, self.client.cookies)
        response = self.client.get(self.current_user_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['authMode'], 'jwt')
        response = self.query('mutation { refreshToken { refreshExpiresIn } }')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('errors', response.json())
        self.assertTrue(response.cookies[jwt_settings.JWT_COOKIE_NAME].value)
        self.assertNotIn(jwt_settings.JWT_REFRESH_TOKEN_COOKIE_NAME, response.cookies)

    def test_logout_recovers_csrf_with_invalid_jwt_and_no_session(self):
        self.client.cookies[jwt_settings.JWT_COOKIE_NAME] = 'invalid'
        response = self.client.get(f'{self.logout_url}csrf/')
        self.assertEqual(response.status_code, 200)
        token = response.json()['csrfToken']
        self.assertEqual(self.client.post(self.logout_url, HTTP_X_CSRFTOKEN=token).status_code, 204)

    @override_settings(CSRF_USE_SESSIONS=False)
    def test_logout_bootstrap_supports_cookie_csrf_without_global_middleware(self):
        middleware = [m for m in settings.MIDDLEWARE if m != 'django.middleware.csrf.CsrfViewMiddleware']
        with override_settings(MIDDLEWARE=middleware):
            self.login_session()
            response = self.client.get(f'{self.logout_url}csrf/')
            token = response.json()['csrfToken']
            self.assertEqual(self.client.post(self.logout_url, HTTP_X_CSRFTOKEN=token).status_code, 204)
            self.assertNotIn(SESSION_KEY, self.client.session)
