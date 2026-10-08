"""Run focused query tests without deployment settings, a database or services."""
import argparse
import sys
from pathlib import Path
from types import ModuleType
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--assembler", required=True, help="Path to openimis-be_py/openIMIS")
parser.add_argument("--location", required=True, help="Path to openimis-be-location_py")
args = parser.parse_args()
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, args.location)
sys.path.insert(0, args.assembler)
from django.conf import settings
settings.configure(SECRET_KEY='test-only', DATABASES={'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}}, INSTALLED_APPS=['django.contrib.auth','django.contrib.contenttypes','django.contrib.sessions','graphene_django','graphql_jwt.refresh_token','axes','core','location'], AUTH_USER_MODEL='core.User', USE_TZ=True, PASSWORD_MIN_LENGTH=8, PASSWORD_UPPERCASE=1, PASSWORD_LOWERCASE=1, PASSWORD_DIGITS=1, PASSWORD_SYMBOLS=1, CACHES={name: {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'} for name in ('default','location')}, MSSQL=False, CACHE_OBJECT_DEFAULT=False, MODE='dev', IS_TESTING=True, SITE_ROOT=lambda: '', ROOT_URLCONF='core.tests.test_query_authentication', GRAPHENE={}, DEFAULT_AUTO_FIELD='django.db.models.AutoField')
# Avoid deployment settings/secrets and startup DB/scheduler in these isolated tests.
m=ModuleType('openIMIS.settings'); m.IS_SENTRY_ENABLED=False; m.DEBUG=False; sys.modules['openIMIS.settings']=m
from core.apps import CoreConfig
CoreConfig.ready=lambda self: None
from location.apps import LocationConfig
LocationConfig.ready=lambda self: None
import django
django.setup()
import unittest
sys.exit(not unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromName('core.tests.test_query_authentication')).wasSuccessful())
