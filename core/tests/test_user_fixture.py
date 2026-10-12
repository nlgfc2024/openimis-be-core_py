"""Fixture reuse must not trigger production password-reuse validation."""
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from core.test_helpers import create_test_interactive_user


class InteractiveUserFixtureTests(SimpleTestCase):
    def make_fixture(self, existing, password_hash, matches):
        interactive = Mock(password=password_hash, private_key='test-key')
        interactive.check_password.return_value = matches
        user = Mock(i_user=interactive)
        with patch('core.test_helpers.InteractiveUser') as model, \
                patch('core.test_helpers.User') as users, \
                patch('core.test_helpers.create_or_update_user_roles'), \
                patch('core.test_helpers.set_current_user'), \
                patch('core.test_helpers.cache'):
            model.objects.filter.return_value.first.return_value = interactive if existing else None
            model.objects.create.return_value = interactive
            users.objects.filter.return_value.first.return_value = user if existing else None
            users.return_value = user
            result = create_test_interactive_user(username='fixture-test', password='Test123!', roles=[1])
        self.assertIs(result, user)
        return interactive

    def test_new_fixture_initializes_password_without_checking_null_hash(self):
        interactive = self.make_fixture(existing=False, password_hash=None, matches=False)
        interactive.check_password.assert_not_called()
        interactive.set_password.assert_called_once_with('Test123!', private_key='test-key')

    def test_existing_matching_password_is_not_reset(self):
        interactive = self.make_fixture(existing=True, password_hash='existing-hash', matches=True)
        interactive.check_password.assert_called_once_with('Test123!')
        interactive.set_password.assert_not_called()

    def test_different_password_still_uses_normal_password_validation(self):
        interactive = self.make_fixture(existing=True, password_hash='existing-hash', matches=False)
        interactive.set_password.assert_called_once_with('Test123!', private_key='test-key')
