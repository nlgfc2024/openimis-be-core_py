import random
from datetime import date


CODE_RANDOM_DIGITS = 5
CODE_GENERATION_ATTEMPTS = 20


def generate_unique_year_code(model, active_filter, current_date=None):
    """Build a `<year><random 5-digit suffix>` code, retrying on collision."""
    year = (current_date or date.today()).year

    for _ in range(CODE_GENERATION_ATTEMPTS):
        suffix = random.randint(0, 10 ** CODE_RANDOM_DIGITS - 1)
        code = f"{year}{suffix:0{CODE_RANDOM_DIGITS}d}"
        if not model.objects.filter(code=code, **active_filter).exists():
            return code

    raise ValueError("Unable to generate a unique code, please retry.")
