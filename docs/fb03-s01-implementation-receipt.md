# FB03-S01 / CCB-11 local implementation receipt

Contract revision 3. Candidate: uncommitted working-tree changes on `codex/ccb11-smoke`, starting HEAD `eb3d522f6561c88057be65c3c149d4ec28849293`. No target branch or PR is assigned. No commit, push, PR, Plane write, runtime configuration change, external message, or delegated agent was performed. No independent acceptance is claimed.

## Bound caller and behavior

Both existing authenticated profile choosers expose POST `/profiles/provision/` (route `provision_profile`). The service `shelf.account_profiles.provision_personal_profile(actor)` accepts only the authenticated actor, creates one active `personal` profile keyed by its account owner, and creates an explicit view/select/mutate grant. Export stays false. POST body account/profile claims do not select authority. The caller redirects to the existing chooser; selection, normal reads, and writes continue to check current grants. Foreign personal profiles deny even with a foreign grant or staff/superuser/installation ownership.

Provisioning owns an IMMEDIATE transaction. Database owner uniqueness prevents a second personal profile. Busy callers receive 503 and reconcile through a later complete call; there is no background retry. Pre-commit interruption rolls back profile and grant together. Lost committed responses return the same profile on readback. An existing owned profile with a missing grant gets that grant; an existing revoked grant remains authoritative. No household slot, feedback, seed, or portable cabinet binding is adopted or created.

Account identity provisioning does not require a synthetic feedback coordinator; the provider runtime deliberately uses its existing account/grant authority. Scoped normal feedback reads/writes retain their existing recovery barrier. Portable authority services are unchanged. Migration 0032 preserves legacy kind uniqueness and existing identities/grants, adds account ownership/shape constraints, and restores the pre-existing portable binding trigger verbatim during SQLite table rebuilds.

## Criterion-to-evidence mapping

| Criterion | Focused evidence | Result/limit |
| --- | --- | --- |
| A1: separate empty owned profiles and explicit grants | `test_two_account_normal_provision_choose_read_write`; `test_provider_chooser_uses_account_scope_and_dynamic_profile_id` | PASS: two normal HTTP provision/chooser/read journeys, below-HTTP feedback read/write, provider intake read/write, empty initial feedback state, explicit grants |
| A2: foreign IDs/grants and installation claims deny | `test_foreign_grant_and_installation_owner_claims_deny_below_http`; provider chooser test | PASS: foreign grants, actual installation owner, staff/superuser, inactive/anonymous account, foreign read/write/selection, revoked mutation grant, submitted owner/profile claims |
| A3: interruption/reload and uniqueness | `test_interruption_rollback_and_lost_response_readback`; `test_concurrent_provision_has_one_owner_generation` | PASS: deterministic failure between profile/grant, committed-response readback, partial owned-profile reconciliation, simultaneous callers, database duplicate rejection |
| Preserve existing schema/portable authority | `test_legacy_identity_grants_and_portable_binding_trigger_survive`; three selected existing GrantAuthorityTests | Migration preservation PASS; inherited portable runtime checks ERROR during socket fixture setup locally |

All fixtures are synthetic, in-memory databases and test-owned temporary directories; Django/fixture teardown completed. The concurrency evidence covers two threads/connections plus the database uniqueness constraint. No process-kill or filesystem power-loss durability test is claimed. Tests use Django clients and services, not an actual browser; human usability and live product acceptance remain unrun. No job/cache/export/deletion acceptance is claimed.

## Exact validation commands and retained outcomes

Available interpreter: `/tmp/ccb16-fb04-s01-v10-venv/bin/python` (Python 3.14.8, Django 5.2.18, matching `requirements.lock`). System `python` could not import Django. Dependency installation was not performed locally. CI installs with `python -m pip install --require-hashes -r requirements.lock` and runs the following command from `backend`:

```sh
python -B manage.py test shelf.tests.test_account_profiles.AccountProfileTests.test_two_account_normal_provision_choose_read_write shelf.tests.test_account_profiles.AccountProfileTests.test_foreign_grant_and_installation_owner_claims_deny_below_http shelf.tests.test_account_profiles.AccountProfileTests.test_interruption_rollback_and_lost_response_readback shelf.tests.test_account_profiles.AccountProfileTests.test_concurrent_provision_has_one_owner_generation shelf.tests.test_account_profiles.AccountProfileTests.test_provider_chooser_uses_account_scope_and_dynamic_profile_id shelf.tests.test_account_profiles.AccountProfileMigrationTests.test_legacy_identity_grants_and_portable_binding_trigger_survive shelf.tests.test_grant_authority.GrantAuthorityTests.test_fresh_binding_is_explicit_repeatable_and_uses_actual_owner shelf.tests.test_grant_authority.GrantAuthorityTests.test_existing_household_profile_is_never_adopted shelf.tests.test_grant_authority.GrantAuthorityTests.test_wrong_owner_revision_session_profile_and_recovery_are_denied --verbosity=2
```

The final local execution used the same arguments with the available interpreter (the script reading the workflow prepended its bin directory to PATH). It was run twice after adding migration acceptance, including once on the final source candidate:

```sh
cd backend
/tmp/ccb16-fb04-s01-v10-venv/bin/python -B manage.py test shelf.tests.test_account_profiles.AccountProfileTests.test_two_account_normal_provision_choose_read_write shelf.tests.test_account_profiles.AccountProfileTests.test_foreign_grant_and_installation_owner_claims_deny_below_http shelf.tests.test_account_profiles.AccountProfileTests.test_interruption_rollback_and_lost_response_readback shelf.tests.test_account_profiles.AccountProfileTests.test_concurrent_provision_has_one_owner_generation shelf.tests.test_account_profiles.AccountProfileTests.test_provider_chooser_uses_account_scope_and_dynamic_profile_id shelf.tests.test_account_profiles.AccountProfileMigrationTests.test_legacy_identity_grants_and_portable_binding_trigger_survive shelf.tests.test_grant_authority.GrantAuthorityTests.test_fresh_binding_is_explicit_repeatable_and_uses_actual_owner shelf.tests.test_grant_authority.GrantAuthorityTests.test_existing_household_profile_is_never_adopted shelf.tests.test_grant_authority.GrantAuthorityTests.test_wrong_owner_revision_session_profile_and_recovery_are_denied --verbosity=2
```

Final result: 9 selected, 6 PASS, 3 ERROR, exit 1. Each portable error occurs in `BoundaryCase.setUp -> open_boundary` before the test body, reporting `BoundaryFailure: CAPABILITY_UNAVAILABLE`. A direct `socket.socket().bind(('127.0.0.1', 0))` probe raised `PermissionError: [Errno 1] Operation not permitted`. These are retained failed runs, not a passing portable verdict; CI has not been executed here.

Before adding migration acceptance, the following 8-test variant ran once: five new account tests PASS and the same three portable fixture errors:

```sh
/tmp/ccb16-fb04-s01-v10-venv/bin/python -B manage.py test shelf.tests.test_account_profiles.AccountProfileTests.test_two_account_normal_provision_choose_read_write shelf.tests.test_account_profiles.AccountProfileTests.test_foreign_grant_and_installation_owner_claims_deny_below_http shelf.tests.test_account_profiles.AccountProfileTests.test_interruption_rollback_and_lost_response_readback shelf.tests.test_account_profiles.AccountProfileTests.test_concurrent_provision_has_one_owner_generation shelf.tests.test_account_profiles.AccountProfileTests.test_provider_chooser_uses_account_scope_and_dynamic_profile_id shelf.tests.test_grant_authority.GrantAuthorityTests.test_fresh_binding_is_explicit_repeatable_and_uses_actual_owner shelf.tests.test_grant_authority.GrantAuthorityTests.test_existing_household_profile_is_never_adopted shelf.tests.test_grant_authority.GrantAuthorityTests.test_wrong_owner_revision_session_profile_and_recovery_are_denied --verbosity=2
```

The new-only command ran once before the final identity-only provisioning adjustment; all 6 passed. All six also passed in the final combined run above:

```sh
/tmp/ccb16-fb04-s01-v10-venv/bin/python -B manage.py test shelf.tests.test_account_profiles.AccountProfileTests.test_two_account_normal_provision_choose_read_write shelf.tests.test_account_profiles.AccountProfileTests.test_foreign_grant_and_installation_owner_claims_deny_below_http shelf.tests.test_account_profiles.AccountProfileTests.test_interruption_rollback_and_lost_response_readback shelf.tests.test_account_profiles.AccountProfileTests.test_concurrent_provision_has_one_owner_generation shelf.tests.test_account_profiles.AccountProfileTests.test_provider_chooser_uses_account_scope_and_dynamic_profile_id shelf.tests.test_account_profiles.AccountProfileMigrationTests.test_legacy_identity_grants_and_portable_binding_trigger_survive --verbosity=2
```

Affected existing provider regression command (from `backend`): all 3 PASS, exit 0:

```sh
/tmp/ccb16-fb04-s01-v10-venv/bin/python -B manage.py test shelf.tests.test_provider_views.ProviderViewsTests.test_login_profile_and_get_have_no_provider_io_or_recovery_claim shelf.tests.test_provider_views.ProviderViewsTests.test_authority_checked_every_action_and_same_selected_profile shelf.tests.test_provider_views.ProviderIntegrationTests.test_exact_old_grant_columns_support_session_actions_and_revocation --verbosity=2
```

An earlier invocation used the nonexistent class `ProviderFormBrowserTests` instead of `ProviderIntegrationTests` for the third method: the first two tests passed, the third selection errored with `AttributeError`. The corrected command above passed. The initial module/class-level selection below was rejected by the repository command boundary before Django ran; the workflow now uses explicit admitted method labels:

```sh
/tmp/ccb16-fb04-s01-v10-venv/bin/python -B manage.py test shelf.tests.test_account_profiles shelf.tests.test_grant_authority.GrantAuthorityTests --verbosity=2
```

From repository root, these checks passed (migration consistency run three times; whitespace check repeated after edits):

```sh
/tmp/ccb16-fb04-s01-v10-venv/bin/python -B backend/manage.py makemigrations --check --dry-run
git diff --check
```

An initial `makemigrations shelf --name account_personal_profile` command was refused because the repository allows only check/dry-run migration commands. Migration 0032 was then authored explicitly; the consistency check reports `No changes detected`.

## Budget and source identity

Gross story diff against the starting HEAD, including new files: **307 physical lines**, comprising **128 production/config/workflow/migration lines** and **179 test lines**. Added plus deleted lines count positively; the generated-code total is zero (migration authored explicitly). No binary/vendor changes. This prose-only receipt is excluded by the contract counting rule. No pre-existing changes were present.

Exact source candidate SHA-256 manifest (receipt excluded):

```text
290673098ece4cb86040adf6056eb896e3e890e63d7fb0e73b378b71161235ef  .github/workflows/fb03-s01-focused.yml
bb3382645d335bf99e09e4d0737ab4f657d6a504af712652c924dbba7f5bb731  backend/cabinet/provider_urls.py
b52605d3794116115ef5b78a3e9ad01ecadc05b962869cfb43c65df63e958621  backend/cabinet/urls.py
c08c2f9afa7b4fb70aedcb83d39208a515800ad1a07b0a8c66fe0295f4871f07  backend/shelf/account_profiles.py
12a53b3b22a2164950ae056f53601aa122269578865d4e4d0cd4e215a38cc9fe  backend/shelf/authority.py
64cfee2accff92fdb0acf1397bf7c5997ea487d09950cfed751bc72122b09fb9  backend/shelf/migrations/0032_account_personal_profile.py
c3c95771dea123a605385a65cf7cf0d3dad3cc27ba8c8439f7c032ce430b32b3  backend/shelf/models.py
938bd181f3805c0329e3416c829cd3606efd863a509d2eec65d6d4d75db4714f  backend/shelf/provider_views.py
5fda989cdbf339bcb45cbcc149356c413bf81fa159bf5ea10d5b83087d180365  backend/shelf/tests/test_account_profiles.py
6e787541161b6b0f9fdc52dd6dde39281e440ab40f986d0089202d8b7c2eba27  backend/shelf/views.py
2c9f018e6bbce69751fd030d39e8cbd2014f54f61b0876745dadc40441b71ead  templates/cabinet/profiles.html
ef5940f0479d8dc86bda976ebd4fbbb254b203a98706c0cf8b23d5267d5904fe  templates/cabinet/provider_profiles.html
```
