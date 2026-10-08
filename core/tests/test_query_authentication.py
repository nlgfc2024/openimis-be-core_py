"""Query gates through the assembler's real GraphQL view (no database fixtures)."""
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import graphene
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied
from django.test import RequestFactory, SimpleTestCase, override_settings
from django.utils.translation import gettext
from graphql_jwt.exceptions import JSONWebTokenError

from core.gql_errors import AuthenticationRequired
from core.schema import OrderedDjangoFilterConnectionField, Query, _check_csrf_token
from core.utils import ExtendedConnection
from openIMIS.views import OpenIMISGraphQLView


class AuthenticationQuery(graphene.ObjectType):
    languages = graphene.List(graphene.String)
    username_length = graphene.Int()
    connection_gate = graphene.Int()
    total_count = graphene.Int()
    edge_count = graphene.Int()
    production_csrf = graphene.String()

    def resolve_production_csrf(self, info):
        _check_csrf_token(info.context)
        return 'ok'

    resolve_languages = Query.resolve_languages
    resolve_username_length = Query.resolve_username_length

    def resolve_connection_gate(self, info):
        return OrderedDjangoFilterConnectionField.resolve_queryset(
            None, None, info, {}, {}, None
        )

    def resolve_total_count(self, info):
        return ExtendedConnection.resolve_total_count(
            SimpleNamespace(length=3), info
        )

    def resolve_edge_count(self, info):
        return ExtendedConnection.resolve_edge_count(
            SimpleNamespace(edges=[1, 2]), info
        )


class QueryHttpTestCase(SimpleTestCase):
    def request(self, query, user=None, session=None, csrf_token=None):
        request = RequestFactory().post(
            '/graphql', json.dumps({'query': query}),
            content_type='application/json',
        )
        request.user = user if user is not None else AnonymousUser()
        request.session = session if session is not None else {}
        if csrf_token is not None:
            request.META['HTTP_X_CSRFTOKEN'] = csrf_token
        return OpenIMISGraphQLView.as_view(
            schema=graphene.Schema(query=AuthenticationQuery), middleware=[]
        )(request)



class QueryAuthenticationTests(QueryHttpTestCase):
    def test_exception_message_and_type(self):
        error = AuthenticationRequired()
        self.assertIsInstance(error, JSONWebTokenError)
        self.assertNotIsInstance(error, PermissionDenied)
        self.assertEqual(str(error), gettext('unauthenticated'))

    def test_anonymous_query_gates_return_http_401(self):
        for field in ('languages', 'connectionGate', 'totalCount', 'edgeCount'):
            with self.subTest(field=field):
                response = self.request('{ ' + field + ' }')
                self.assertEqual(response.status_code, 401)
                messages = [e['message'] for e in json.loads(response.content)['errors']]
                self.assertIn(gettext('unauthenticated'), messages)

    @patch('core.schema.Language.objects')
    def test_authenticated_query_succeeds(self, languages):
        languages.order_by.return_value.all.return_value = ['English']
        user = SimpleNamespace(is_authenticated=True)
        response = self.request('{ languages totalCount edgeCount }', user)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content), {
            'data': {'languages': ['English'], 'totalCount': 3, 'edgeCount': 2},
        })
        languages.order_by.assert_called_once_with('sort_order')

    def test_authenticated_permission_denial_is_not_http_401(self):
        user = SimpleNamespace(is_authenticated=True, has_perms=Mock(return_value=False))
        response = self.request('{ usernameLength }', user)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content)['errors'][0]['message'], gettext('unauthorized'))
        user.has_perms.assert_called_once()

    def test_permission_gate_still_raises_permission_denied(self):
        user = SimpleNamespace(is_authenticated=True, has_perms=Mock(return_value=False))
        with self.assertRaises(PermissionDenied):
            Query.resolve_username_length(None, SimpleNamespace(context=SimpleNamespace(user=user)))


@override_settings(MODE='prod', IS_TESTING=False, USER_AGENT_CSRF_BYPASS=[])
class ProductionCsrfTests(QueryHttpTestCase):
    def test_matching_production_csrf_token_allows_request(self):
        response = self.request(
            '{ productionCsrf }', SimpleNamespace(is_authenticated=True),
            session={'csrftoken': 'test-session-token'}, csrf_token='test-session-token',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content), {'data': {'productionCsrf': 'ok'}})

    def test_expired_production_session_returns_exact_legacy_error(self):
        response = self.request(
            '{ productionCsrf }', SimpleNamespace(is_authenticated=True),
            session={}, csrf_token='stale-session-token',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.content)['errors'][0]['message'], "'csrftoken'")

    def test_incorrect_production_csrf_token_remains_rejected(self):
        response = self.request(
            '{ productionCsrf }', SimpleNamespace(is_authenticated=True),
            session={'csrftoken': 'test-session-token'}, csrf_token='incorrect',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            json.loads(response.content)['errors'][0]['message'],
            'CSRF token missing or incorrect.',
        )
