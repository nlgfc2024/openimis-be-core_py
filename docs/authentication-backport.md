# Mlatho authentication backport: openIMIS backend #447

Upstream: https://github.com/openimis/openimis-be-core_py/pull/447
Frontend behavior: https://github.com/openimis/openimis-fe-core_js/pull/343

## Baseline and adaptation

Base `mw/develop`: `67ce392` in `nlgfc2024/openimis-be-core_py`.
The original checkout was clean and remains untouched; implementation uses a separate worktree.
All four query authentication gates still raised `PermissionDenied`. No equivalent backport issue/PR existed.

Attempted sequential `git cherry-pick -x` of `7b52c3f78252b2e256490a5e69f6402a521e6c92`,
`e07d486a92a4e77a698fe73c6164664c952d2468`, and `de0077be13a672371153dde9c54a4c2a624621d8`.
The first commit conflicted on `core/schema.py` (upstream's additional `ObjectDoesNotExist` import)
and `core/tests/test_gql_queries.py` (absent in the fork; upstream also includes unrelated constraint tests).
Aborted the sequence and manually reproduced the final diff without those unrelated changes.

- `core/gql_errors.py`: `AuthenticationRequired(JSONWebTokenError)` with translated default message.
- `core/schema.py`: only `resolve_queryset` and `resolve_languages` authentication gates change.
- `core/utils.py`: only `ExtendedConnection` total/edge count authentication gates change.
- `locale/en/LC_MESSAGES/django.po`: English `unauthenticated` translation.
- `core/tests/test_query_authentication.py`: five tests using real resolver methods and assembler HTTP view.
- `tools/test_query_authentication.py`: isolated test runner, no database, deployment settings or services.

Existing authenticated permission checks, custom code generation, mutations and cross-module authorization are unchanged.
Neither upstream #446 nor frontend #354 is included.

## Verification

Executed with Python 3.12, Django 4.2.30, django-graphql-jwt 0.3.4,
graphene-django 2.16.0 and graphql-core 2.3.2, installed from the assembler requirements
and this module's declared dependencies in `/tmp/mlatho-auth-venv`.
No dependency or lockfile changes are part of this PR.

```sh
/tmp/mlatho-auth-venv/bin/python tools/test_query_authentication.py \
  --assembler /home/yutaka/MSR_2026/mlatho/backend/openimis-be_py/openIMIS \
  --location /tmp/mlatho-auth-location
/tmp/mlatho-auth-venv/bin/flake8 core/gql_errors.py core/tests/test_query_authentication.py --ignore E501
python3 -m compileall -q core/gql_errors.py core/schema.py core/utils.py core/tests/test_query_authentication.py tools/test_query_authentication.py
git diff --check
```

All passed: five test methods (anonymous test exercises all four gates), focused lint, compilation and whitespace checks.
Expected GraphQL exception traces appear during negative tests; they are not failures.
The test runner disables AppConfig startup hooks and Sentry settings, injects request users, and mocks only the language database lookup.
It exercises the real GraphQL executor and `OpenIMISGraphQLView` HTTP response, not a replacement status mapper.
The full database-backed core suite and real cookie/JWT authentication middleware were **not run**.
The translation assertion uses `gettext`; compilation of the PO catalog was **not run** (`msgfmt` unavailable).

## Assembler requirement and remaining integration tests

The local assembler at `a75741e127d729806642bfb41574625f0e0293ed` already implements
`has_jwt_error` and maps a GraphQL error's `original_error: JSONWebTokenError` to HTTP 401
in `openIMIS/openIMIS/views.py`. The test uses that file directly.
Location schema dependency for isolated testing: `nlgfc2024/openimis-be-location_py` `mw/develop`,
`888df7fdfbfd50ad56cee939a0e0075189d80cd5`; cloned read-only for tests, no changes published.
An assembler using vanilla Graphene view may still return HTTP 200; validate the deployed assembler before activation.

Before marking ready, use an isolated assembled environment and test:

1. Anonymous POST `/api/graphql`, `{ languages { name } }`: 401 with translated authentication error.
2. Valid Django session and valid JWT separately: 200 and expected language data.
3. Expired JWT: 401; valid refresh cookie restores access after one refresh.
4. Authenticated user without user/role query permission: permission error, never converted to 401.
5. Anonymous connection and count fields: 401 where these core gates execute.
6. Run the normal `python manage.py test core.tests.test_query_authentication` and database-backed core regression suite.

The reusable CI configuration references the fork workflow, but that workflow can clone upstream
assembler branches as a fallback. Its result is not a substitute for testing the actual Mlatho assembly.
Keep the PR draft until database-backed authentication and frontend integration are validated.
