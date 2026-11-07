"""Test-only template contract. M owns all actual UI files outside backend/."""
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "APP_DIRS": False,
    "OPTIONS": {"loaders": [("django.template.loaders.locmem.Loader", {
        "registration/login.html": (
            '<form method="post" action="/login/">{% csrf_token %}{{ form.as_p }}'
            '<button type="submit">Login</button></form>'
        ),
        "cabinet/profiles.html": (
            '{% for profile in profile_options %}'
            '<form method="post" action="/profiles/select/">{% csrf_token %}'
            '<button name="profile_id" value="{{ profile.id }}">{{ profile.label }}</button>'
            '</form>{% endfor %}'
            '<form method="post" action="/logout/">{% csrf_token %}</form>'
        ),
        "cabinet/catalogue.html": (
            '<h1>{{ selected_profile.label }}</h1>'
            '{% for card in cards %}<article>{{ card.title }} {{ card.category }} '
            '{{ card.description }}</article>{% endfor %}'
            '<form method="post" action="/logout/">{% csrf_token %}</form>'
        ),
    })]},
}]
