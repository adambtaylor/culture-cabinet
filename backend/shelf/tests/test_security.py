from io import StringIO
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import SESSION_KEY, get_user_model
from django.contrib.sessions.models import Session
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import Client, TestCase, override_settings
from django.urls import Resolver404, resolve

from cabinet.errors import bad_request, forbidden, server_error, unavailable
from shelf.authority import SELECTION_KEY
from shelf.catalogue import CARDS, Card
from shelf.models import Grant, Profile
from shelf.tests.fixtures import TEMPLATES

PASSWORD = "Fictional-test-sundial-47!"


@override_settings(TEMPLATES=TEMPLATES, ALLOWED_HOSTS=["testserver"])
class SecurityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.one = User.objects.create_user("fictional-juniper", password=PASSWORD)
        cls.two = User.objects.create_user("fictional-clover", password=PASSWORD)
        # Active isolated fixtures ensure inactivity cannot mask a grant defect.
        cls.owner = Profile.objects.create(kind="owner", label="Juniper Lantern", active=True)
        cls.partner = Profile.objects.create(kind="partner", label="Clover Comet", active=True)
        cls.family = Profile.objects.create(kind="family", label="Pebble Chorus", active=True)
        Grant.objects.create(user=cls.one, profile=cls.owner, can_view=True, can_select=True)
        Grant.objects.create(user=cls.two, profile=cls.partner, can_view=True, can_select=True)

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.client.force_login(self.one)

    def token(self, client=None):
        client = client or self.client
        client.get("/profiles/")
        return client.cookies[settings.CSRF_COOKIE_NAME].value

    def select(self, target, **extra):
        return self.client.post("/profiles/select/", {
            "csrfmiddlewaretoken": self.token(), "profile_id": target, **extra,
        })

    def assert_denied(self, response):
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.content, b"Not available.\n")
        self.assertEqual(response["Content-Type"], "text/plain")
        self.assertEqual(response["Cache-Control"], "no-store")
        for profile in (self.owner, self.partner, self.family):
            self.assertNotIn(profile.label.encode(), response.content)

    def test_two_active_users_have_isolated_reads_and_selection(self):
        other = Client(enforce_csrf_checks=True)
        other.force_login(self.two)
        for client, own, foreign in ((self.client, self.owner, self.partner),
                                     (other, self.partner, self.owner)):
            response = client.get("/profiles/")
            self.assertContains(response, own.label)
            self.assertNotContains(response, foreign.label)
            self.assertNotContains(response, self.family.label)
            self.assertEqual(client.get(f"/profiles/{own.pk}/").status_code, 200)
            self.assert_denied(client.get(f"/profiles/{foreign.pk}/"))
            token = self.token(client)
            self.assertEqual(client.post("/profiles/select/", {
                "profile_id": own.pk, "csrfmiddlewaretoken": token,
            }).status_code, 302)
            self.assertEqual(client.session[SELECTION_KEY], own.pk)
            self.assert_denied(client.post("/profiles/select/", {
                "profile_id": foreign.pk, "csrfmiddlewaretoken": token,
            }))
            self.assertEqual(client.session[SELECTION_KEY], own.pk)
            self.assertContains(client.get("/"), own.label)

    def test_unknown_unauthorized_and_inactive_are_equivalent(self):
        self.family.active = False
        self.family.save()
        Grant.objects.create(user=self.one, profile=self.family, can_view=True, can_select=True)
        for path in ("/profiles/999999/", f"/profiles/{self.partner.pk}/",
                     f"/profiles/{self.family.pk}/"):
            response = self.client.get(path)
            self.assert_denied(response)
            self.assertEqual(dict(response.headers), dict(self.client.get("/profiles/999998/").headers))
        for target in (999999, self.partner.pk, self.family.pk, "bogus", "9" * 100, "-1", "", "١"):
            self.assert_denied(self.select(target))
            self.assertNotIn(SELECTION_KEY, self.client.session)

    def test_missing_and_duplicate_target_are_denied(self):
        token = self.token()
        self.assert_denied(self.client.post("/profiles/select/", {"csrfmiddlewaretoken": token}))
        self.assert_denied(self.client.post("/profiles/select/", {
            "profile_id": [str(self.owner.pk), str(self.partner.pk)], "csrfmiddlewaretoken": token,
        }))
        self.assertNotIn(SELECTION_KEY, self.client.session)

    def test_forged_actor_and_target_never_confer_authority(self):
        self.assert_denied(self.select(self.partner.pk, actor_id=self.two.pk, user_id=self.two.pk))
        self.assert_denied(self.client.get(f"/profiles/{self.partner.pk}/", {
            "actor_id": self.two.pk, "user_id": self.two.pk,
        }, HTTP_X_ACTOR_ID=str(self.two.pk)))
        session = self.client.session
        session["actor_id"] = self.two.pk
        session["user_id"] = self.two.pk
        session[SELECTION_KEY] = self.partner.pk
        session.save()
        self.assert_denied(self.client.get("/", {"profile_id": self.owner.pk}))

    def test_superuser_does_not_bypass_missing_membership(self):
        self.one.is_superuser = True
        self.one.is_staff = True
        self.one.save()
        self.assert_denied(self.select(self.partner.pk))
        self.assert_denied(self.client.get(f"/profiles/{self.partner.pk}/"))

    def test_action_permissions_are_independent_and_selection_requires_view(self):
        grant = Grant.objects.get(user=self.one, profile=self.owner)
        grant.can_select = False
        grant.save()
        self.assertEqual(self.client.get(f"/profiles/{self.owner.pk}/").status_code, 200)
        self.assert_denied(self.select(self.owner.pk))
        grant.can_select, grant.can_view = True, False
        grant.save()
        self.assert_denied(self.select(self.owner.pk))
        self.assert_denied(self.client.get(f"/profiles/{self.owner.pk}/"))
        self.assertNotContains(self.client.get("/profiles/"), self.owner.label)

    def test_view_revocation_denies_next_selected_and_direct_request(self):
        self.assertEqual(self.select(self.owner.pk).status_code, 302)
        Grant.objects.filter(user=self.one, profile=self.owner).update(can_view=False)
        self.assert_denied(self.client.get("/"))
        self.assert_denied(self.client.get(f"/profiles/{self.owner.pk}/"))
        self.assert_denied(self.select(self.owner.pk))

    def test_deleted_membership_is_not_cached_in_session(self):
        self.select(self.owner.pk)
        Grant.objects.filter(user=self.one, profile=self.owner).delete()
        self.assert_denied(self.client.get("/"))
        self.assert_denied(self.select(self.owner.pk))

    def test_select_revocation_is_checked_again_but_view_grant_still_allows_view(self):
        self.select(self.owner.pk)
        Grant.objects.filter(user=self.one, profile=self.owner).update(can_select=False)
        self.assert_denied(self.select(self.owner.pk))
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_deactivation_and_reactivation_never_restore_revoked_membership(self):
        self.select(self.owner.pk)
        Profile.objects.filter(pk=self.owner.pk).update(active=False)
        self.assert_denied(self.client.get("/"))
        self.assert_denied(self.client.get(f"/profiles/{self.owner.pk}/"))
        self.assert_denied(self.select(self.owner.pk))
        Grant.objects.filter(user=self.one, profile=self.owner).delete()
        Profile.objects.filter(pk=self.owner.pk).update(active=True)
        self.assert_denied(self.client.get("/"))
        self.assert_denied(self.select(self.owner.pk))

    def test_user_deactivation_removes_session_authority(self):
        self.select(self.owner.pk)
        get_user_model().objects.filter(pk=self.one.pk).update(is_active=False)
        self.assertEqual(self.client.get("/").status_code, 302)
        self.assertEqual(self.client.get(f"/profiles/{self.owner.pk}/").status_code, 302)

    def test_failed_selection_preserves_authorized_context(self):
        self.select(self.owner.pk)
        self.assert_denied(self.select(self.partner.pk))
        self.assertEqual(self.client.session[SELECTION_KEY], self.owner.pk)
        self.assertContains(self.client.get("/"), self.owner.label)

    def test_no_get_mutations_or_post_reads(self):
        self.select(self.owner.pk)
        key = self.client.session.session_key
        for path in ("/profiles/select/", "/logout/"):
            self.assertEqual(self.client.get(path, {"profile_id": self.partner.pk}).status_code, 405)
        self.assertEqual(self.client.session[SELECTION_KEY], self.owner.pk)
        self.assertEqual(self.client.session.session_key, key)
        token = self.token()
        for path in ("/", "/profiles/", f"/profiles/{self.owner.pk}/"):
            self.assertEqual(self.client.post(path, {"csrfmiddlewaretoken": token}).status_code, 405)
        self.client.get(f"/profiles/{self.partner.pk}/")
        self.assertEqual(self.client.session[SELECTION_KEY], self.owner.pk)

    def test_csrf_protects_selection_and_logout_with_missing_invalid_and_foreign_origin(self):
        self.select(self.owner.pk)
        key = self.client.session.session_key
        token = self.token()
        for path in ("/profiles/select/", "/logout/"):
            for data in ({"profile_id": self.owner.pk},
                         {"profile_id": self.owner.pk, "csrfmiddlewaretoken": "invalid"}):
                response = self.client.post(path, data)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.content, b"Request rejected.\n")
            self.assertEqual(self.client.post(path, {
                "profile_id": self.owner.pk, "csrfmiddlewaretoken": token,
            }, HTTP_ORIGIN="https://fictional.invalid").status_code, 403)
        self.assertEqual(self.client.session.session_key, key)
        self.assertEqual(self.client.session[SELECTION_KEY], self.owner.pk)

    def test_no_membership_pooling_between_users_or_login_sessions(self):
        self.select(self.owner.pk)
        token = self.token()
        self.client.post("/logout/", {"csrfmiddlewaretoken": token})
        self.client.get("/login/")
        token = self.client.cookies[settings.CSRF_COOKIE_NAME].value
        self.client.post("/login/", {"username": self.two.username,
                                     "password": PASSWORD, "csrfmiddlewaretoken": token})
        self.assertNotIn(SELECTION_KEY, self.client.session)
        self.assert_denied(self.client.get(f"/profiles/{self.owner.pk}/"))

    def test_anonymous_actor_fields_do_not_authenticate(self):
        anonymous = Client(enforce_csrf_checks=True)
        for path in ("/", "/profiles/", f"/profiles/{self.owner.pk}/"):
            response = anonymous.get(path, {"actor_id": self.one.pk})
            self.assertEqual(response.status_code, 302)
            self.assertNotIn(self.owner.label.encode(), response.content)

    def test_catalogue_is_shared_finite_and_direct_read_does_not_change_selection(self):
        self.select(self.owner.pk)
        Grant.objects.create(user=self.one, profile=self.partner, can_view=True)
        response = self.client.get(f"/profiles/{self.partner.pk}/")
        self.assertEqual(self.client.session[SELECTION_KEY], self.owner.pk)
        self.assertEqual(response.context["cards"], CARDS)
        self.assertEqual(len(CARDS), 3)
        self.assertEqual(self.client.get("/").context["cards"], CARDS)

    def test_hostile_profile_and_card_text_are_escaped(self):
        hostile = '<script>alert("fictional")</script><img src=x onerror=alert(1)>'
        Profile.objects.filter(pk=self.owner.pk).update(label=hostile)
        self.assertContains(self.client.get("/profiles/"), "&lt;script&gt;")
        with patch("shelf.views.CARDS", (Card(hostile, hostile, hostile),)):
            response = self.client.get(f"/profiles/{self.owner.pk}/")
        self.assertNotContains(response, "<script>")
        self.assertNotContains(response, "<img src=x")
        self.assertContains(response, "&lt;script&gt;")

    def test_admin_exports_and_static_backend_files_are_absent(self):
        for path in ("/admin/", "/export/", "/exports/", "/profiles/1/export/",
                     "/api/export/"):
            with self.assertRaises(Resolver404):
                resolve(path)
            self.assert_denied(self.client.get(path))

    def test_real_asset_route_rejects_traversal_and_secret_store_requests(self):
        for path in ("/static/../backend/cabinet/settings.py", "/static/session-key",
                     "/static/synthetic.sqlite3", "/static/../.demo-data/session-key",
                     "/static/%2e%2e/.demo-data/synthetic.sqlite3", "/static/",
                     "/static//etc/passwd"):
            self.assert_denied(self.client.get(path))

    def test_security_headers_deny_remote_assets_scripts_and_caching(self):
        for path in ("/login/", "/profiles/", "/profiles/999999/"):
            response = self.client.get(path)
            self.assertEqual(response["Cache-Control"], "no-store")
            self.assertEqual(response["X-Frame-Options"], "DENY")
            self.assertEqual(response["X-Content-Type-Options"], "nosniff")
            policy = response["Content-Security-Policy"]
            for rule in ("default-src 'none'", "connect-src 'none'", "script-src 'none'",
                         "form-action 'self'", "frame-ancestors 'none'"):
                self.assertIn(rule, policy)

    def test_unlisted_host_is_rejected_and_forwarded_host_cannot_change_authority(self):
        response = self.client.get("/profiles/", HTTP_HOST="fictional.invalid")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.content, b"Request rejected.\n")
        response = self.client.get("/profiles/", HTTP_X_FORWARDED_HOST="fictional.invalid")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.owner.label)

    def test_fictional_secret_canaries_never_reach_responses_or_errors(self):
        secret = "SYNTHETIC-SECRET-CANARY-never-render-829"
        get_user_model().objects.filter(pk=self.one.pk).update(email=secret)
        with override_settings(SECRET_KEY=secret):
            # Reauthenticate after key change; never expose credentials or auth internals.
            self.client.force_login(self.one)
            self.select(self.owner.pk)
            responses = [self.client.get(path) for path in (
                "/login/", "/profiles/", "/", f"/profiles/{self.owner.pk}/",
                f"/profiles/{self.partner.pk}/", "/missing/", "/static/session-key",
            )]
            responses.append(self.client.post("/profiles/select/", {"profile_id": secret}))
            with patch("shelf.views.render", side_effect=RuntimeError(secret)):
                errors = Client(enforce_csrf_checks=True, raise_request_exception=False)
                errors.force_login(self.one)
                responses.append(errors.get("/profiles/"))
                self.assertEqual(responses[-1].status_code, 500)
        responses.extend(handler(None, Exception(secret)) for handler in (
            bad_request, forbidden, unavailable,
        ))
        responses.append(server_error(None))
        for response in responses:
            wire = response.content + str(dict(response.headers)).encode()
            self.assertNotIn(secret.encode(), wire)
            self.assertNotIn(PASSWORD.encode(), wire)
            self.assertNotIn(self.one.password.encode(), wire)


@override_settings(TEMPLATES=TEMPLATES, ALLOWED_HOSTS=["testserver"])
class AuthenticationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user("fictional-auth", password=PASSWORD)

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)

    def login_data(self):
        self.client.get("/login/")
        return {"username": self.user.username, "password": PASSWORD,
                "csrfmiddlewaretoken": self.client.cookies[settings.CSRF_COOKIE_NAME].value}

    def test_login_csrf_and_bad_credentials_leave_anonymous_session(self):
        data = self.login_data()
        for token in (None, "bad"):
            payload = dict(data)
            if token is None:
                payload.pop("csrfmiddlewaretoken")
            else:
                payload["csrfmiddlewaretoken"] = token
            self.assertEqual(self.client.post("/login/", payload).status_code, 403)
            self.assertNotIn(SESSION_KEY, self.client.session)
        data["password"] = "fictional-wrong-password"
        self.assertEqual(self.client.post("/login/", data).status_code, 200)
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_login_rotates_session_and_csrf_logout_invalidates_replay(self):
        session = self.client.session
        session["synthetic_marker"] = "rotation-check"
        session.save()
        old_key = session.session_key
        data = self.login_data()
        old_csrf = self.client.cookies[settings.CSRF_COOKIE_NAME].value
        response = self.client.post("/login/?next=https://fictional.invalid/", data)
        self.assertRedirects(response, "/", fetch_redirect_response=False)
        authenticated_key = self.client.session.session_key
        self.assertNotEqual(authenticated_key, old_key)
        self.assertFalse(Session.objects.filter(session_key=old_key).exists())
        self.assertNotEqual(self.client.cookies[settings.CSRF_COOKIE_NAME].value, old_csrf)
        self.assertEqual(self.client.session[SESSION_KEY], str(self.user.pk))
        token = self.client.cookies[settings.CSRF_COOKIE_NAME].value
        self.assertEqual(self.client.post("/logout/", {"csrfmiddlewaretoken": token}).status_code, 302)
        self.assertFalse(Session.objects.filter(session_key=authenticated_key).exists())
        self.assertNotIn(SESSION_KEY, self.client.session)
        replay = Client(enforce_csrf_checks=True)
        replay.cookies[settings.SESSION_COOKIE_NAME] = authenticated_key
        self.assertEqual(replay.get("/profiles/").status_code, 302)


class BootstrapTests(TestCase):
    def bootstrap(self, **kwargs):
        with patch("shelf.management.commands.demo_bootstrap.getpass", side_effect=[PASSWORD, PASSWORD]):
            call_command("demo_bootstrap", synthetic_demo=True, stdout=StringIO(), **kwargs)

    def test_bootstrap_exactly_three_slots_only_owner_active_and_password_hashed(self):
        self.bootstrap()
        self.assertEqual(list(Profile.objects.values_list("kind", "active")), [
            ("owner", True), ("partner", False), ("family", False),
        ])
        user = get_user_model().objects.get()
        self.assertEqual(user.username, "demo-curator")
        self.assertTrue(user.check_password(PASSWORD))
        self.assertNotEqual(user.password, PASSWORD)
        self.assertFalse(user.is_staff or user.is_superuser)
        self.assertEqual(Grant.objects.count(), 1)
        grant = Grant.objects.get()
        self.assertEqual(grant.profile.kind, "owner")
        self.assertTrue(grant.can_view and grant.can_select)
        client = Client(enforce_csrf_checks=True)
        client.force_login(user)
        for target in Profile.objects.filter(active=False):
            # Even an explicit grant cannot make an inactive shipped slot accessible.
            Grant.objects.create(user=user, profile=target, can_view=True, can_select=True)
            with override_settings(TEMPLATES=TEMPLATES, ALLOWED_HOSTS=["testserver"]):
                client.get("/profiles/")
                token = client.cookies[settings.CSRF_COOKIE_NAME].value
                self.assertEqual(client.post("/profiles/select/", {
                    "profile_id": target.pk, "csrfmiddlewaretoken": token,
                }).status_code, 404)
                self.assertEqual(client.get(f"/profiles/{target.pk}/").status_code, 404)

    def test_bootstrap_requires_explicit_mode_and_nonempty_valid_matching_password(self):
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            call_command("demo_bootstrap", stdout=StringIO())
        for passwords in (("", ""), (PASSWORD, "mismatch"), ("short", "short")):
            with patch("shelf.management.commands.demo_bootstrap.getpass", side_effect=passwords):
                with self.assertRaises(CommandError):
                    call_command("demo_bootstrap", synthetic_demo=True, stdout=StringIO())
            self.assertFalse(Profile.objects.exists())
            self.assertFalse(get_user_model().objects.exists())

    def test_bootstrap_never_resets_existing_data(self):
        from django.core.management.base import CommandError
        self.bootstrap()
        with self.assertRaises(CommandError):
            self.bootstrap()
        self.assertEqual(Profile.objects.count(), 3)
        self.assertEqual(get_user_model().objects.count(), 1)

    def test_database_constraints_reject_fourth_kind_duplicate_slot_and_grant(self):
        self.bootstrap()
        for kwargs in ({"kind": "together", "label": "Invalid fiction"},
                       {"kind": "owner", "label": "Duplicate fiction"}):
            with self.assertRaises(IntegrityError), transaction.atomic():
                Profile.objects.create(**kwargs)
        grant = Grant.objects.get()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Grant.objects.create(user=grant.user, profile=grant.profile)
