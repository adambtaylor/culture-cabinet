"""Invented current-use grants; no provider permission or household mutation."""
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from unittest import TestCase

from shelf.tmdb_contract import (
    AdapterResult, ContextAction, Coverage, Decision, FieldProvenance, Observation,
    Overview, PolicyEvidence, SourceIdentity, TmdbPolicy, VideoFact,
    check_policy, project_for_use,
)

NOW = datetime(2026, 10, 27, tzinfo=UTC)
ALLOW = PolicyEvidence('allow', NOW, 'fictional-evidence', 'Invented only')
DENY = PolicyEvidence('deny', NOW)
UNKNOWN = PolicyEvidence('unknown', NOW)
DISPLAY = 'display_catalog_fields'


class TmdbPolicyTests(TestCase):
    def setUp(self):
        self.identity = SourceIdentity('movie', 900000001, True)
        self.coverage = Coverage('discover/movie', 'movie')
        self.provenance = {name: FieldProvenance('discover/movie', 'a'*64, 'b'*64,
                            '/' + name, NOW, True) for name in
                           ('source_identity', 'title', 'overview', 'release', 'checked_at')}
        self.observation = Observation(source_identity=self.identity, domain_kind='film',
            title='FICTIONAL TITLE', overview=Overview('text', 'Invented overview'), checked_at=NOW,
            payload_sha256='b'*64, field_provenance=self.provenance,
            rights_snapshot=TmdbPolicy('old'), coverage=self.coverage)
        self.permissions = {name: ALLOW for name in (*self.provenance, 'field_provenance')}
        self.policy = TmdbPolicy('current', operations={DISPLAY: ALLOW,
            'provider_content_export': DENY, 'external_AI_model_input': DENY},
            field_permissions={DISPLAY: self.permissions})

    def test_unknown_defaults_and_synthetic_grants_do_not_authorize_provider_data(self):
        for policy, fictional in ((TmdbPolicy('unknown'), True), (self.policy, False)):
            decision = check_policy(current_policy=policy, operation=DISPLAY, fictional=fictional)
            self.assertEqual(decision, Decision('blocked', 'rights_unknown'))

    def test_policy_precedence_off_pause_deny_unknown(self):
        for off, pause, permission, reason in (
                (True, True, DENY, 'source_off'), (False, True, DENY, 'paused'),
                (False, False, DENY, 'rights_denied'), (False, False, UNKNOWN, 'rights_unknown')):
            with self.subTest(reason=reason):
                policy = TmdbPolicy('x', off, pause, {DISPLAY: permission})
                self.assertEqual(check_policy(current_policy=policy, operation=DISPLAY,
                    fictional=True), Decision('blocked', reason))
        self.assertIsNone(check_policy(current_policy=self.policy, operation=DISPLAY, fictional=True))

    def test_display_independent_of_unknown_retention(self):
        projection = project_for_use(self.observation, operation=DISPLAY, current_policy=self.policy)
        self.assertEqual(projection.fields['title'], 'FICTIONAL TITLE')
        self.assertEqual(projection.current_policy_version, 'current')
        self.assertEqual(dict(projection.provenance), self.provenance)
        self.assertEqual(check_policy(current_policy=self.policy,
            operation='retain_identity_metadata', fictional=True), Decision('blocked', 'rights_unknown'))

    def test_export_and_ai_release_no_source_fields(self):
        for operation in ('provider_content_export', 'external_AI_model_input'):
            result = project_for_use(self.observation, operation=operation, current_policy=self.policy)
            self.assertEqual(result, Decision('blocked', 'rights_denied'))
            self.assertFalse(hasattr(result, 'fields'))

    def test_required_identity_title_or_provenance_denial_blocks(self):
        for name in ('source_identity', 'title', 'field_provenance'):
            for permission in (DENY, UNKNOWN):
                with self.subTest(name=name, permission=permission.decision):
                    permissions = dict(self.permissions, **{name: permission})
                    policy = replace(self.policy, field_permissions={DISPLAY: permissions})
                    self.assertEqual(project_for_use(self.observation, operation=DISPLAY,
                        current_policy=policy), Decision('blocked', 'required_fields_not_permitted'))

    def test_optional_unknown_or_denied_overview_strips_only_overview(self):
        for permission in (UNKNOWN, DENY):
            policy = replace(self.policy, field_permissions={DISPLAY:
                dict(self.permissions, overview=permission)})
            projection = project_for_use(self.observation, operation=DISPLAY, current_policy=policy)
            self.assertNotIn('overview', projection.fields)
            self.assertNotIn('overview', projection.provenance)
            self.assertEqual(projection.fields['title'], self.observation.title)
            self.assertEqual(projection.provenance['title'], self.provenance['title'])

    def test_missing_required_provenance_blocks_and_unknown_optional_provenance_strips(self):
        for missing in ('title', 'source_identity'):
            observation = replace(self.observation, field_provenance={
                k: v for k, v in self.provenance.items() if k != missing})
            self.assertEqual(project_for_use(observation, operation=DISPLAY, current_policy=self.policy),
                             Decision('blocked', 'required_fields_not_permitted'))
        observation = replace(self.observation, field_provenance={
            k: v for k, v in self.provenance.items() if k != 'overview'})
        self.assertNotIn('overview', project_for_use(observation, operation=DISPLAY,
                        current_policy=self.policy).fields)

    def test_current_version_revokes_previous_projection_without_relabeling(self):
        first = project_for_use(self.observation, operation=DISPLAY, current_policy=self.policy)
        revoked = replace(self.policy, version='revoked', operations={DISPLAY: DENY})
        self.assertEqual(project_for_use(self.observation, operation=DISPLAY, current_policy=revoked),
                         Decision('blocked', 'rights_denied'))
        self.assertEqual(first.current_policy_version, 'current')
        self.assertEqual(self.observation.rights_snapshot.version, 'old')
        self.assertEqual(first.provenance['title'].fictional, True)

    def test_policy_and_fact_mapping_inputs_cannot_mutate_frozen_outputs(self):
        self.permissions['title'] = DENY
        self.provenance.clear()
        projection = project_for_use(self.observation, operation=DISPLAY, current_policy=self.policy)
        self.assertEqual(projection.fields['title'], 'FICTIONAL TITLE')
        for mapping in (self.policy.operations, self.policy.field_permissions[DISPLAY],
                        self.observation.field_provenance, projection.fields, projection.provenance):
            with self.assertRaises(TypeError):
                mapping['title'] = 'changed'
        with self.assertRaises(FrozenInstanceError):
            self.observation.title = 'changed'

    def test_video_requires_current_key_site_type_official_identity_and_provenance(self):
        names = ('source_identity', 'key', 'site', 'type', 'official')
        provenance = {name: FieldProvenance('movie/videos', 'a'*64, 'b'*64,
                        '/' + name, NOW, True) for name in names}
        fact = VideoFact(key='Fiction_001', site='Invented site', type='Trailer', official=True,
            source_identity=self.identity, checked_at=NOW, provenance=provenance,
            payload_sha256='b'*64, rights_snapshot=self.policy, coverage=self.coverage)
        permissions = {name: ALLOW for name in (*names, 'provenance')}
        policy = replace(self.policy, field_permissions={DISPLAY: permissions})
        self.assertEqual(project_for_use(fact, operation=DISPLAY, current_policy=policy).fields['official'], True)
        for name in permissions:
            denied = replace(policy, field_permissions={DISPLAY: dict(permissions, **{name: DENY})})
            self.assertEqual(project_for_use(fact, operation=DISPLAY, current_policy=denied),
                             Decision('blocked', 'required_fields_not_permitted'))

    def test_identity_media_separation_and_exact_integer_bounds(self):
        self.assertNotEqual(self.identity, SourceIdentity('tv', self.identity.id, True))
        self.assertEqual(SourceIdentity('movie', 2**63-1, True).id, 2**63-1)
        for value in (True, False, 0, -1, 2**63, 1.0, '1'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                SourceIdentity('movie', value, True)

    def test_fixture_action_and_blocked_result_have_no_household_authority(self):
        action = ContextAction(reason='fixture')
        self.assertIsNone(action.href)
        self.assertIsNone(action.destination_checked_at)
        self.assertFalse(action.ownership_verified)
        for policy in (replace(self.policy, source_off=True), replace(self.policy, paused=True)):
            result = project_for_use(self.observation, operation=DISPLAY, current_policy=policy)
            self.assertIsInstance(result, Decision)
            self.assertFalse(hasattr(result, 'observations'))
        result = AdapterResult(outcome='blocked', reason='source_off', attempted_at=NOW,
            coverage=self.coverage, rights_snapshot=self.policy)
        self.assertEqual(result.observations, ())
        self.assertEqual(result.videos, ())
