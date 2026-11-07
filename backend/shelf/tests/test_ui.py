"""Synthetic UI contracts exercised against the project's actual templates."""

from html.parser import HTMLParser
import json
from unittest.mock import patch
import uuid

from django.conf import settings
from django.contrib.auth import SESSION_KEY, get_user_model
from django.db import connection
from django.test import Client, TestCase
from django.urls import reverse
from django.utils.html import escape, strip_tags

from cabinet.boundary import ROOT
from shelf.authority import SELECTION_KEY
from shelf.catalogue import CARDS, TARGET_IDS, Card
from shelf.models import (
    FeedbackEvent,
    FeedbackProjection,
    FeedbackTarget,
    Grant,
    Profile,
)
from shelf.portability import import_legacy_fixture
from shelf.tests.synthetic_feedback import (
    FeedbackCase,
    binding_for_fixture,
    legacy_fixture,
)


class Markup(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


class RealTemplateUI(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.password = "Entirely-fictional-UI-test-49!"
        cls.user = get_user_model().objects.create_user(
            "demo-curator", password=cls.password
        )
        cls.owner = Profile.objects.create(
            kind="owner", label="Juniper Lantern", active=True
        )
        cls.foreign = Profile.objects.create(
            kind="partner", label="Hidden Fictional Grove", active=True
        )
        cls.inactive = Profile.objects.create(
            kind="family", label="Hidden Fictional Reed", active=False
        )
        Grant.objects.create(
            user=cls.user, profile=cls.owner, can_view=True, can_select=True
        )
        Grant.objects.create(
            user=cls.user, profile=cls.inactive, can_view=True, can_select=True
        )

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True, HTTP_HOST="127.0.0.1")

    def token(self):
        return self.client.cookies[settings.CSRF_COOKIE_NAME].value

    def html(self, response):
        self.assertEqual(response.status_code, 200)
        text = response.content.decode()
        tags = Markup(text).tags
        self.assertIn("Synthetic demo", text)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertIn("script-src 'none'", response["Content-Security-Policy"])
        for tag, attrs in tags:
            self.assertNotIn(
                tag, ("script", "iframe", "object", "embed", "img", "style")
            )
            self.assertFalse(any((k.startswith("on") for k in attrs)))
            for attr in ("href", "src", "action"):
                if attr in attrs:
                    self.assertTrue(attrs[attr].startswith(("/", "#")), attrs[attr])
                    self.assertFalse(attrs[attr].startswith("//"))
        forms = [(t, a) for t, a in tags if t == "form"]
        self.assertTrue(forms)
        self.assertTrue(all((a.get("method") == "post" for _, a in forms)))
        tokens = [
            a
            for t, a in tags
            if t == "input" and a.get("name") == "csrfmiddlewaretoken"
        ]
        self.assertEqual(len(tokens), len(forms))
        self.assertTrue(all((a.get("value") for a in tokens)))
        for hidden in (self.foreign.label, self.inactive.label):
            self.assertNotIn(hidden, text)
        return (text, tags)

    def login(self):
        self.client.get("/login/")
        response = self.client.post(
            "/login/",
            {
                "username": self.user.username,
                "password": self.password,
                "csrfmiddlewaretoken": self.token(),
            },
        )
        self.assertEqual((response.status_code, response.url), (302, "/"))

    def test_login_labels_csrf_field_and_nonfield_errors(self):
        text, tags = self.html(self.client.get("/login/"))
        labels = {a.get("for") for t, a in tags if t == "label"}
        self.assertTrue({"id_username", "id_password"} <= labels)
        for field in ("username", "password"):
            self.assertTrue(
                any(
                    (
                        t == "input"
                        and a.get("name") == field
                        and (a.get("id") in labels)
                        for t, a in tags
                    )
                )
            )
        self.assertEqual(
            self.client.post("/login/", {"username": "demo-curator"}).status_code, 403
        )
        text, tags = self.html(
            self.client.post("/login/", {"csrfmiddlewaretoken": self.token()})
        )
        self.assertIn("This field is required.", text)
        self.assertIn('role="alert"', text)
        self.assertIn('id="id_username_error"', text)
        text, _ = self.html(
            self.client.post(
                "/login/",
                {
                    "username": "demo-curator",
                    "password": "fictional-wrong",
                    "csrfmiddlewaretoken": self.token(),
                },
            )
        )
        self.assertIn("Please enter a correct username and password", text)
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_selection_cards_isolation_logout_and_local_css(self):
        self.login()
        text, tags = self.html(self.client.get("/profiles/"))
        self.assertIn(self.owner.label, text)
        choices = [a["value"] for t, a in tags if a.get("name") == "profile_id"]
        self.assertEqual(choices, [str(self.owner.pk)])
        self.assertEqual(
            self.client.post(
                "/profiles/select/", {"profile_id": self.owner.pk}
            ).status_code,
            403,
        )
        selected = self.client.post(
            "/profiles/select/",
            {"profile_id": choices[0], "csrfmiddlewaretoken": self.token()},
        )
        self.assertEqual((selected.status_code, selected.url), (302, "/"))
        text, tags = self.html(self.client.get("/"))
        self.assertEqual(sum((t == "article" for t, _ in tags)), 3)
        for card in CARDS:
            for value in (card.title, card.category, card.description):
                self.assertIn(value, text)
        self.assertTrue(
            any((t == "a" and a.get("href") == "/profiles/" for t, a in tags))
        )
        self.assertTrue(
            any((t == "form" and a.get("action") == "/logout/" for t, a in tags))
        )
        for path in (
            f"/profiles/{self.foreign.pk}/",
            f"/profiles/{self.inactive.pk}/",
            "/profiles/999999/",
        ):
            denial = self.client.get(path)
            self.assertEqual(
                (denial.status_code, denial.content), (404, b"Not available.\n")
            )
        denied = self.client.post(
            "/profiles/select/",
            {"profile_id": self.foreign.pk, "csrfmiddlewaretoken": self.token()},
        )
        self.assertEqual(denied.status_code, 404)
        self.assertEqual(self.client.session[SELECTION_KEY], self.owner.pk)
        links = [a["href"] for t, a in tags if t == "link"]
        self.assertEqual(links, ["/static/cabinet.css"])
        css = self.client.get(links[0])
        self.assertEqual(css.status_code, 200)
        content = b"".join(css.streaming_content).decode()
        self.assertEqual(content, (ROOT / "static/cabinet.css").read_text())
        self.assertIn(":focus-visible", content)
        self.assertIn("@media", content)
        for forbidden in (
            "@import",
            "url(",
            "animation:",
            "transition:",
            "http:",
            "https:",
        ):
            self.assertNotIn(forbidden, content)
        self.assertEqual(self.client.get("/logout/").status_code, 405)
        self.assertEqual(self.client.post("/logout/", {}).status_code, 403)
        out = self.client.post("/logout/", {"csrfmiddlewaretoken": self.token()})
        self.assertEqual((out.status_code, out.url), (302, "/login/"))
        self.assertNotIn(SESSION_KEY, self.client.session)
        self.assertEqual(self.client.get("/profiles/").status_code, 302)

    def test_hostile_fiction_and_neutral_empty_state(self):
        self.login()
        hostile = '<script>fictional("ink")</script><img src=x onerror=fictional()>'
        Profile.objects.filter(pk=self.owner.pk).update(label=hostile)
        text, _ = self.html(self.client.get("/profiles/"))
        self.assertIn(str(escape(hostile)), text)
        with patch(
            "shelf.views.CARDS",
            tuple((Card(hostile, hostile, hostile) for _ in range(3))),
        ):
            text, _ = self.html(self.client.get(f"/profiles/{self.owner.pk}/"))
        self.assertGreaterEqual(text.count(str(escape(hostile))), 10)
        Grant.objects.filter(user=self.user, profile=self.owner).delete()
        text, tags = self.html(self.client.get("/profiles/"))
        self.assertIn("No profile available", text)
        self.assertNotIn(str(escape(hostile)), text)
        self.assertFalse(any((a.get("name") == "profile_id" for _, a in tags)))


class Forms(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.forms = []
        self.current = None
        self.visible = []
        self.ids = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if "id" in a:
            self.ids.append(a["id"])
        if tag == "form":
            self.current = {"action": a.get("action"), "data": {}}
            self.forms.append(self.current)
        if self.current is not None and tag == "input":
            self.current["data"][a.get("name")] = a.get("value", "")
        if tag in ("input", "select", "textarea") and a.get("type") != "hidden":
            self.visible.append(a)

    def handle_endtag(self, tag):
        if tag == "form":
            self.current = None


class FeedbackUI(FeedbackCase):
    def setUp(self):
        super().setUp()
        self.client = Client(enforce_csrf_checks=True, HTTP_HOST="127.0.0.1")
        self.client.force_login(self.actor)
        self.url = reverse("feedback_detail", args=[self.profile.pk, TARGET_IDS[0]])
        self.export = reverse("feedback_export", args=[self.profile.pk])
        self.foreign = Profile.objects.create(
            kind="partner", label="Private fictional grove", active=True
        )
        self.inactive = Profile.objects.create(
            kind="family", label="Inactive fictional reed", active=False
        )
        Grant.objects.create(
            user=self.actor,
            profile=self.inactive,
            can_view=True,
            can_select=True,
            can_mutate=True,
            can_export=True,
        )

    def page(self, url=None, status=200):
        response = self.client.get(url or self.url)
        self.assertEqual(response.status_code, status)
        if status != 200:
            return response
        self.markup(response)
        return response

    def markup(self, response):
        text = response.content.decode()
        tags = Markup(text).tags
        self.assertIn("Synthetic demo", text)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertIn("script-src 'none'", response["Content-Security-Policy"])
        for tag, a in tags:
            self.assertNotIn(
                tag, ("script", "iframe", "object", "embed", "img", "style")
            )
            self.assertFalse(any((k.startswith("on") for k in a)))
            for attr in ("href", "src", "action"):
                if attr in a:
                    self.assertTrue(
                        a[attr].startswith(("/", "#"))
                        and (not a[attr].startswith("//"))
                    )
        forms = Forms(text)
        self.assertEqual(len(forms.ids), len(set(forms.ids)))
        for f in forms.forms:
            self.assertTrue(f["data"].get("csrfmiddlewaretoken"))
        for profile in (self.foreign, self.inactive):
            self.assertNotIn(profile.label, text)
        return forms

    def form(self, signal, response=None, operation="set", index=0):
        response = response or self.page()
        choices = [
            f
            for f in Forms(response.content.decode()).forms
            if f["data"].get("signal") == signal
            and f["data"].get("operation") == operation
        ]
        self.assertGreater(len(choices), index)
        return choices[index]

    def submit(self, f, **values):
        return self.client.post(f["action"], {**f["data"], **values})

    def test_create_correct_undo_two_signals_and_repeat(self):
        response = self.page()
        enjoy = self.form("enjoyment", response)
        interest = self.form("interest", response)
        self.assertFalse(connection.in_atomic_block)
        self.assertEqual(self.submit(enjoy, rating="4").status_code, 303)
        self.assertEqual(self.submit(enjoy, rating="4").status_code, 303)
        self.assertEqual(self.submit(interest, choice="maybe").status_code, 303)
        self.assertEqual(FeedbackEvent.objects.count(), 2)
        text = self.page().content.decode()
        self.assertIn("4 out of 5", text)
        self.assertIn("Anticipated interest", text)
        correction = self.form("enjoyment")
        self.assertTrue(correction["data"]["corrects_id"])
        self.assertEqual(self.submit(correction, rating="2").status_code, 303)
        undo = self.form("enjoyment", operation="reverse")
        self.assertEqual(self.submit(undo).status_code, 303)
        self.assertEqual(self.submit(undo).status_code, 303)
        self.assertIn("4 out of 5", self.page().content.decode())
        self.assertEqual(
            FeedbackProjection.objects.get(signal="interest").value["value"], "maybe"
        )

    def test_conflict_and_validation_preserve_submitted_identity(self):
        stale = self.form("enjoyment")
        fresh = self.form("enjoyment")
        self.assertEqual(self.submit(fresh, rating="5").status_code, 303)
        conflict = self.submit(stale, rating="1")
        self.assertEqual(conflict.status_code, 409)
        self.markup(conflict)
        self.assertIn("refresh", conflict.content.decode().lower())
        rebound = self.form("enjoyment", conflict)
        self.assertEqual(
            {k: v for k, v in rebound["data"].items() if k != "csrfmiddlewaretoken"},
            {k: v for k, v in stale["data"].items() if k != "csrfmiddlewaretoken"},
        )
        invalid = self.submit(rebound, rating="9")
        self.assertEqual(invalid.status_code, 400)
        self.markup(invalid)
        self.assertIn('role="alert"', invalid.content.decode())
        self.assertIn('aria-invalid="true"', invalid.content.decode())
        self.assertEqual(
            self.form("enjoyment", invalid)["data"]["mutation_id"],
            stale["data"]["mutation_id"],
        )
        self.assertEqual(
            self.form("enjoyment", invalid)["data"]["base_revision"],
            stale["data"]["base_revision"],
        )
        retry = self.form("enjoyment")
        bad = self.submit(retry, rating="9")
        self.assertEqual(
            self.submit(self.form("enjoyment", bad), rating="3").status_code, 303
        )
        self.assertEqual(FeedbackEvent.objects.count(), 2)

    def test_saved_hidden_unhidden_and_no_incidental_enjoyment(self):
        self.assertEqual(
            self.submit(self.form("saved"), checked="true").status_code, 303
        )
        self.assertFalse(FeedbackEvent.objects.filter(signal="enjoyment").exists())
        summary = self.page(
            reverse("profile_catalogue", args=[self.profile.pk])
        ).content.decode()
        self.assertIn("Saved:", summary)
        self.assertIn("Yes", summary)
        self.assertNotIn("{&#x27;value&#x27;", summary)
        hidden = self.form("visibility")
        self.assertEqual(self.submit(hidden, choice="hidden").status_code, 303)
        catalogue = self.page(
            reverse("profile_catalogue", args=[self.profile.pk])
        ).content.decode()
        self.assertNotIn(self.url, catalogue)
        text = self.page().content.decode()
        self.assertIn("Hidden for this profile", text)
        self.assertIn("also hides its sessions", text)
        self.assertEqual(
            self.submit(self.form("visibility"), choice="visible").status_code, 303
        )
        self.assertIn(
            self.url,
            self.page(
                reverse("profile_catalogue", args=[self.profile.pk])
            ).content.decode(),
        )

    def test_csrf_methods_and_revoked_permissions(self):
        f = self.form("saved")
        no_token = dict(f["data"])
        no_token.pop("csrfmiddlewaretoken")
        self.assertEqual(
            self.client.post(f["action"], {**no_token, "checked": "true"}).status_code,
            403,
        )
        self.assertEqual(self.client.get(f["action"]).status_code, 405)
        Grant.objects.filter(pk=self.grant.pk).update(
            can_mutate=False, can_export=False
        )
        text = self.page().content.decode()
        forms = Forms(text)
        self.assertFalse(any((f["data"].get("signal") for f in forms.forms)))
        self.assertNotIn(self.export, text)
        self.assertEqual(self.submit(f, checked="true").status_code, 404)
        self.assertEqual(self.client.get(self.export).status_code, 404)
        Grant.objects.filter(pk=self.grant.pk).update(can_view=False)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_unauthorized_inactive_anonymous_and_profile_selection(self):
        self.page("/profiles/")
        for profile in (self.foreign, self.inactive):
            for name, args in [
                ("feedback_detail", [profile.pk, TARGET_IDS[0]]),
                ("feedback_export", [profile.pk]),
            ]:
                self.assertEqual(
                    self.client.get(reverse(name, args=args)).status_code, 404
                )
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_export_formats_errors_no_automatic_download_and_grant_retry(self):
        response = self.page(self.export)
        self.assertNotIn("Content-Disposition", response)
        text = response.content.decode()
        for word in ("Markdown", "human-readable", "JSON", "CSV", "other profiles"):
            self.assertIn(word, text)
        form = next((f for f in Forms(text).forms if f["action"] == self.export))
        self.assertEqual(
            self.client.post(self.export, {"format": "md"}).status_code, 403
        )
        invalid = self.submit(form, format="xml")
        self.assertEqual(invalid.status_code, 400)
        self.markup(invalid)
        other = Grant.objects.create(
            user=self.actor,
            profile=self.foreign,
            can_view=True,
            can_mutate=True,
            can_export=True,
        )
        self.assertEqual(
            self.submit(
                self.form("constraint"), text="Only this fictional profile"
            ).status_code,
            303,
        )
        from shelf.feedback import mutate
        from dataclasses import replace

        mutate(
            actor=self.actor,
            profile_id=self.foreign.pk,
            command=replace(
                self.command("constraint", {"text": "OTHER PROFILE PRIVATE FICTION"}),
                base_revision=0,
                context_event_id=None,
            ),
        )
        for fmt in ("md", "json", "csv"):
            out = self.submit(form, format=fmt)
            self.assertEqual(out.status_code, 200)
            self.assertIn("attachment", out["Content-Disposition"])
            self.assertEqual(out["Cache-Control"], "no-store")
            self.assertIn("Only this fictional profile", out.content.decode())
            for secret in ("OTHER PROFILE PRIVATE FICTION", "session-key", "password"):
                self.assertNotIn(secret, out.content.decode())
        Grant.objects.filter(pk=self.grant.pk).update(can_export=False)
        self.assertEqual(self.submit(form, format="md").status_code, 404)

    def test_escaped_constraint_and_legacy_basis_and_clear(self):
        hostile = "<script>fiction()</script>"
        self.assertEqual(
            self.submit(self.form("constraint"), text=hostile).status_code, 303
        )
        text = self.page().content.decode()
        self.assertIn(str(escape(hostile)), text)
        self.assertNotIn(hostile, text)
        self.assertEqual(self.submit(self.form("constraint"), text="").status_code, 303)
        self.assertIn("No current value.", self.page().content.decode())

    def test_legacy_unknown_basis_and_attributed_prose(self):
        from shelf.feedback import canonical

        fixture = legacy_fixture()
        import_legacy_fixture(
            actor=self.actor,
            profile_id=self.profile.pk,
            fixture_bytes=canonical(fixture),
            binding=binding_for_fixture(self.profile, fixture),
            mutation_id=str(uuid.uuid4()),
        )
        target = (
            FeedbackProjection.objects.filter(
                signal="legacy_rating", value__isnull=False
            )
            .first()
            .target
        )
        response = self.page(
            reverse("feedback_detail", args=[self.profile.pk, target.pk])
        )
        text = response.content.decode()
        self.assertIn("Basis unknown", text)
        self.assertIn("LOW → HIGH", text)
        self.assertIn("not a statement of experienced enjoyment", text)

    def test_other_typed_signals_never_imply_enjoyment(self):
        for signal, values in [
            ("familiarity", {"choice": "aware"}),
            ("completion", {"choice": "in_progress"}),
            ("skipped", {"checked": "true"}),
            ("opened", {"checked": "true"}),
        ]:
            self.assertEqual(self.submit(self.form(signal), **values).status_code, 303)
        text = self.page().content.decode()
        self.assertIn("In progress", text)
        self.assertIn("Aware", text)
        self.assertFalse(FeedbackEvent.objects.filter(signal="enjoyment").exists())
        self.assertEqual(
            set(FeedbackEvent.objects.values_list("signal", flat=True)),
            {"familiarity", "completion", "skipped", "opened"},
        )

    def test_hidden_parent_session_scope_and_undo_eligibility(self):
        session = FeedbackTarget.objects.create(
            id=uuid.uuid4(),
            kind="session",
            parent_id=TARGET_IDS[0],
            title="Fictional session",
            category="Session",
            source_namespace="synthetic-test",
            source_key="ui-session",
        )
        url = reverse("feedback_detail", args=[self.profile.pk, session.pk])
        session_form = self.form("saved", self.page(url))
        self.assertEqual(session_form["data"]["scope"], "session")
        self.assertEqual(self.submit(session_form, checked="true").status_code, 303)
        self.assertEqual(
            self.submit(self.form("visibility"), choice="hidden").status_code, 303
        )
        self.assertIn("Hidden for this profile", self.page(url).content.decode())
        self.assertEqual(
            self.submit(self.form("visibility", operation="reverse")).status_code, 303
        )
        self.assertNotIn("Hidden for this profile", self.page(url).content.decode())
        rows = Forms(self.page().content.decode()).forms
        self.assertFalse(
            any(
                (
                    f["data"].get("signal") == "visibility"
                    and f["data"].get("operation") == "reverse"
                    for f in rows
                )
            )
        )

    def test_stale_undo_preserves_command_and_revoked_retry_denied(self):
        self.assertEqual(
            self.submit(self.form("saved"), checked="true").status_code, 303
        )
        undo = self.form("saved", operation="reverse")
        self.assertEqual(
            self.submit(self.form("saved"), checked="false").status_code, 303
        )
        response = self.submit(undo)
        self.assertEqual(response.status_code, 409)
        self.markup(response)
        rebound = self.form("saved", response, operation="reverse")
        for name in ("mutation_id", "base_revision", "reverses_id", "context_event_id"):
            self.assertEqual(rebound["data"][name], undo["data"][name])
        self.assertIn("Undo Saved", response.content.decode())
        Grant.objects.filter(pk=self.grant.pk).update(can_mutate=False)
        self.assertEqual(self.submit(rebound).status_code, 404)
        self.assertEqual(FeedbackEvent.objects.count(), 2)

    def test_invalid_hidden_context_has_human_error_guidance(self):
        form = self.form("saved")
        response = self.submit(form, base_revision="invalid", checked="true")
        self.assertEqual(response.status_code, 400)
        self.markup(response)
        text = response.content.decode()
        self.assertIn("This form could not be verified", text)
        for jargon in ("Base revision", "Mutation id", "Context event id"):
            self.assertNotIn(jargon, text)
        rebound = self.form("saved", response)
        self.assertEqual(rebound["data"]["mutation_id"], form["data"]["mutation_id"])
        self.assertEqual(rebound["data"]["base_revision"], "invalid")
        self.assertFalse(FeedbackEvent.objects.exists())

    def test_empty_catalogue_after_explicit_hiding(self):
        for target in TARGET_IDS:
            url = reverse("feedback_detail", args=[self.profile.pk, target])
            self.assertEqual(
                self.submit(
                    self.form("visibility", self.page(url)), choice="hidden"
                ).status_code,
                303,
            )
        self.assertIn(
            "No works visible",
            self.page(
                reverse("profile_catalogue", args=[self.profile.pk])
            ).content.decode(),
        )

    def statement(self, label, response=None):

        class Statements(HTMLParser):
            def __init__(self, html):
                super().__init__()
                self.active = False
                self.part = None
                self.label = ""
                self.values = {}
                self.feed(html)

            def handle_starttag(self, tag, attrs):
                if tag == "dl" and dict(attrs).get("class") == "statements":
                    self.active = True
                if self.active and tag in ("dt", "dd"):
                    self.part = tag
                    self.text = []

            def handle_data(self, data):
                if self.part:
                    self.text.append(data)

            def handle_endtag(self, tag):
                if tag == self.part:
                    text = " ".join("".join(self.text).split())
                    if tag == "dt":
                        self.label = text
                    else:
                        self.values[self.label] = text
                    self.part = None
                if tag == "dl":
                    self.active = False

        response = response or self.page()
        return Statements(response.content.decode()).values[label]

    def import_source_statement(self):
        from shelf.feedback import canonical

        fixture = legacy_fixture(1)
        import_legacy_fixture(
            actor=self.actor,
            profile_id=self.profile.pk,
            fixture_bytes=canonical(fixture),
            binding=binding_for_fixture(self.profile, fixture),
            mutation_id=str(uuid.uuid4()),
        )
        event = FeedbackEvent.objects.get(signal="saved")
        self.url = reverse("feedback_detail", args=[self.profile.pk, event.target_id])

    def ban_source(self):
        from shelf.recovery import restrict_authority, trusted_operator

        restrict_authority(
            actor=trusted_operator(self.recovery, self.actor),
            recovery=self.recovery,
            command=dict(
                mutation_id=str(uuid.uuid4()),
                base_sequence=self.recovery.sequence,
                restrictions=[
                    dict(
                        kind="source_restrict",
                        profile_id=None,
                        target_id=None,
                        signal=None,
                        scope=None,
                        subject_id="legacy-fixture-v1",
                        restriction_code="deny_source_content",
                    )
                ],
            ),
        )

    def export_markdown(self):
        response = self.page(self.export)
        form = next(
            (
                f
                for f in Forms(response.content.decode()).forms
                if f["action"] == self.export
            )
        )
        exported = self.submit(form, format="md")
        self.assertEqual(exported.status_code, 200)
        return exported

    def test_explicit_clear_and_revision_zero_render_separately(self):
        self.assertEqual(self.statement("Practical constraint"), "Not recorded.")
        self.assertEqual(
            self.submit(
                self.form("constraint"), text="Fictional quiet room"
            ).status_code,
            303,
        )
        self.assertEqual(self.submit(self.form("constraint"), text="").status_code, 303)
        self.assertEqual(self.statement("Practical constraint"), "No current value.")
        self.assertEqual(self.statement("Saved"), "Not recorded.")
        exported = self.export_markdown()
        self.assertIn(b"Explicitly cleared", exported.content)
        self.assertNotIn(b"Source content redacted", exported.content)
        self.assertIn("fiction-v1.md", exported["Content-Disposition"])

    def test_source_redaction_renders_neutral_detail_and_explicit_export(self):
        self.import_source_statement()
        self.assertEqual(self.statement("Saved"), "Yes")
        self.ban_source()
        self.assertEqual(self.statement("Saved"), "No current value.")
        text = self.page().content.decode()
        self.assertNotIn("A fictional attributed statement.", text)
        self.assertNotIn("Cleared; no current value.", text)
        exported = self.export_markdown()
        self.assertIn(b"Source content redacted", exported.content)
        self.assertNotIn(b"Explicitly cleared", exported.content)
        self.assertNotIn(b"A fictional attributed statement.", exported.content)
        self.assertIn("fiction-v2.md", exported["Content-Disposition"])

    def test_human_correction_survives_ban_and_reversal_reveals_null(self):
        self.import_source_statement()
        self.assertEqual(
            self.submit(self.form("saved"), checked="false").status_code, 303
        )
        self.ban_source()
        self.assertEqual(self.statement("Saved"), "No")
        self.assertEqual(
            self.submit(self.form("saved", operation="reverse")).status_code, 303
        )
        self.assertEqual(self.statement("Saved"), "No current value.")
        self.assertIn(b"Source content redacted", self.export_markdown().content)
        self.assertNotIn("Cleared; no current value.", self.page().content.decode())

    def test_personal_erasure_renders_neutral_and_omits_erased_summary(self):
        from shelf.feedback import erase_feedback, EraseCommand, key_token

        self.assertEqual(
            self.submit(
                self.form("constraint"), text="Fictional erased constraint"
            ).status_code,
            303,
        )
        event = FeedbackEvent.objects.get(signal="constraint")
        token = key_token(str(event.target_id), "constraint", "item")
        erase_feedback(
            actor=self.actor,
            profile_id=self.profile.pk,
            recovery=self.recovery,
            command=EraseCommand(
                str(uuid.uuid4()),
                str(event.target_id),
                "item",
                ("constraint",),
                {token: 1},
                {token: str(event.pk)},
                "ERASE_FICTIONAL_FEEDBACK",
            ),
        )
        response = self.page()
        self.assertEqual(
            self.statement("Practical constraint", response), "No current value."
        )
        self.assertNotIn("Fictional erased constraint", response.content.decode())
        self.assertFalse(
            any(
                (
                    f["data"].get("signal") == "constraint"
                    for f in Forms(response.content.decode()).forms
                )
            )
        )
        exported = self.export_markdown()
        for value in (
            b"Fictional erased constraint",
            b"Explicitly cleared",
            b"Source content redacted",
        ):
            self.assertNotIn(value, exported.content)
        self.assertIn("fiction-v1.md", exported["Content-Disposition"])

    def test_catalogue_and_detail_are_neutral_after_undo_and_erasure(self):
        from shelf.feedback import EraseCommand, erase_feedback, key_token

        initial = self.page()
        for label in ("Saved", "Practical constraint"):
            self.assertEqual(self.statement(label, initial), "Not recorded.")

        self.assertEqual(
            self.submit(self.form("saved"), checked="true").status_code, 303
        )
        self.assertEqual(
            self.submit(self.form("saved", operation="reverse")).status_code, 303
        )
        self.assertEqual(
            self.submit(
                self.form("constraint"), text="Fictional erased constraint"
            ).status_code,
            303,
        )
        event = FeedbackEvent.objects.get(signal="constraint")
        token = key_token(str(event.target_id), "constraint", "item")
        erase_feedback(
            actor=self.actor,
            profile_id=self.profile.pk,
            recovery=self.recovery,
            command=EraseCommand(
                str(uuid.uuid4()),
                str(event.target_id),
                "item",
                ("constraint",),
                {token: 1},
                {token: str(event.pk)},
                "ERASE_FICTIONAL_FEEDBACK",
            ),
        )

        self.assertEqual(
            list(
                FeedbackEvent.objects.filter(signal="saved")
                .order_by("revision")
                .values_list("operation", flat=True)
            ),
            ["set", "reverse"],
        )
        for signal in ("saved", "constraint"):
            self.assertIsNone(FeedbackProjection.objects.get(signal=signal).value)
        self.assertTrue(FeedbackProjection.objects.get(signal="constraint").erased)

        export_page = self.page(self.export)
        export_form = next(
            f
            for f in Forms(export_page.content.decode()).forms
            if f["action"] == self.export
        )
        exported = self.submit(export_form, format="json")
        self.assertEqual(exported.status_code, 200)
        records = json.loads(exported.content)["records"]
        saved = [r for r in records if r["signal"] == "saved"]
        erased = [r for r in records if r["signal"] == "constraint"]
        self.assertEqual([r["operation"] for r in saved], ["set", "reverse"])
        self.assertTrue(
            all(not r["erased"] and not r.get("source_redacted", False) for r in saved)
        )
        self.assertEqual([r["operation"] for r in erased], ["set", "erase"])
        for record in erased:
            self.assertTrue(record["erased"])
            self.assertIsNone(record["value"])
            self.assertFalse(record.get("source_redacted", False))
        self.assertNotIn(b"Fictional erased constraint", exported.content)

        detail = self.page()
        for label in ("Saved", "Practical constraint"):
            self.assertEqual(self.statement(label, detail), "No current value.")
        self.assertNotContains(detail, "Cleared; no current value.")
        catalogue = self.page(reverse("profile_catalogue", args=[self.profile.pk]))
        self.assertContains(catalogue, "No current value.", count=2)
        summary = " ".join(strip_tags(catalogue.content.decode()).split())
        for label in ("Saved", "Practical constraint"):
            self.assertIn(f"{label}: No current value.", summary)
        self.assertNotContains(catalogue, "Cleared; no current value.")
