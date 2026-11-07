"""Login middleware regression checks using actual templates and in-memory data."""
from django.conf import settings
from django.contrib.auth import SESSION_KEY, get_user_model
from django.test import Client, TestCase


class BrowserLoginTests(TestCase):
    origin = "http://127.0.0.1:8000"
    password = "Fictional-browser-regression-73!"

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            "fictional-browser-login", password=cls.password,
        )

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True, HTTP_HOST="127.0.0.1:8000")

    def login_form(self):
        response = self.client.get("/login/")
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "registration/login.html")
        self.assertIn(settings.CSRF_COOKIE_NAME, self.client.cookies)
        token = str(response.context["csrf_token"])
        self.assertEqual(len(token), 64)
        self.assertContains(response, token)
        return response, {
            "username": self.user.username,
            "password": self.password,
            "csrfmiddlewaretoken": token,
        }

    def assert_login_rejected(self, response):
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.content, b"Request rejected.\n")
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_actual_login_response_referrer_policy(self):
        response, _ = self.login_form()
        self.assertEqual(response["Referrer-Policy"], "same-origin")

    def test_same_origin_with_valid_csrf_authenticates(self):
        _, data = self.login_form()
        response = self.client.post("/login/", data, HTTP_ORIGIN=self.origin)
        self.assertRedirects(response, "/", fetch_redirect_response=False)
        self.assertEqual(self.client.session[SESSION_KEY], str(self.user.pk))
        self.assertEqual(response["Referrer-Policy"], "same-origin")

    def test_same_origin_without_csrf_is_rejected(self):
        _, data = self.login_form()
        del data["csrfmiddlewaretoken"]
        self.assert_login_rejected(self.client.post("/login/", data, HTTP_ORIGIN=self.origin))

    def test_null_origin_with_valid_csrf_is_rejected(self):
        _, data = self.login_form()
        self.assert_login_rejected(self.client.post("/login/", data, HTTP_ORIGIN="null"))

    def test_foreign_origin_with_valid_csrf_is_rejected(self):
        _, data = self.login_form()
        self.assert_login_rejected(self.client.post(
            "/login/", data, HTTP_ORIGIN="http://fictional.invalid:8000",
        ))
