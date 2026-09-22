from datetime import date
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from core.code_generation import generate_unique_year_code


class GenerateUniqueYearCodeTest(SimpleTestCase):
    def setUp(self):
        self.model = MagicMock()
        self.active_filter = {"is_deleted": False}

    @patch("core.code_generation.random.randint", return_value=12345)
    def test_generates_year_and_five_digit_suffix(self, _):
        self.model.objects.filter.return_value.exists.return_value = False

        code = generate_unique_year_code(
            self.model, self.active_filter, current_date=date(2026, 1, 1)
        )

        self.assertEqual(code, "202612345")
        self.model.objects.filter.assert_called_once_with(
            code="202612345", is_deleted=False
        )

    @patch("core.code_generation.random.randint", side_effect=[12345, 67890])
    def test_retries_after_a_collision(self, _):
        self.model.objects.filter.return_value.exists.side_effect = [True, False]

        code = generate_unique_year_code(
            self.model, self.active_filter, current_date=date(2026, 1, 1)
        )

        self.assertEqual(code, "202667890")
        self.assertEqual(self.model.objects.filter.call_count, 2)

    def test_raises_after_all_attempts_collide(self):
        self.model.objects.filter.return_value.exists.return_value = True

        with self.assertRaisesMessage(ValueError, "Unable to generate a unique code"):
            generate_unique_year_code(
                self.model, self.active_filter, current_date=date(2026, 1, 1)
            )
