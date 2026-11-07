"""Separate portable URLconf; legacy entrypoints keep their original routes."""
from django.urls import path
from shelf import portable_setup_views as views
from shelf import portable_connection_views as connections
from shelf import onboarding_experience_views as experience
from shelf import enhancement_views as enhance

urlpatterns = [
    path('setup/cabinet', views.cabinet_entry),
    path('setup/cabinet/create', views.create_cabinet),
    path('setup/cabinet/bind', views.bind_cabinet),
    path('cabinet', enhance.portable_cabinet),
    path('cabinet/consent', enhance.portable_inference_consent),
    path('cabinet/consent/change', enhance.portable_inference_consent_change),
    path('enhance/presentation.css', enhance.portable_enhancement_style),
    path('enhance', enhance.portable_page),
    path('enhance/style.css', enhance.portable_style),
    path('enhance/start', enhance.portable_start),
    path('enhance/runs/<uuid:run_ref>/status', enhance.portable_status),
    path('enhance/runs/<uuid:run_ref>/preview', enhance.portable_preview),
    path('enhance/runs/<uuid:run_ref>/confirm', enhance.portable_confirm),
    path('enhance/runs/<uuid:run_ref>/cancel', enhance.portable_cancel),
    path('enhance/current/<uuid:profile_ref>', enhance.portable_current),
    path('setup/enhance/status', views.enhance_status),
    path("", experience.setup),
    path("setup", experience.setup),
    path("setup/style.css", experience.style),
    *[path("setup/form/" + name, view) for name, view in [
        ("claim", views.html_claim), ("login", views.html_login),
        ("logout", views.html_logout), ("reauthenticate", views.html_reauthenticate),
        ("configure", connections.html_configure), ("credential", connections.html_credential),
        ("remove", connections.html_remove), ("preview", connections.html_preview),
        ("confirm", connections.html_confirm),
    ]],
    path('setup/status', views.status),
    path('setup/connections/configure', connections.configure),
    path('setup/connections/credential', connections.credential),
    path('setup/connections/remove', connections.remove),
    path('setup/connections/preview', connections.preview),
    path('setup/connections/confirm', connections.confirm),
    path('setup/connections/status', connections.status),
    path('setup/claim', views.claim),
    path('setup/login', views.login),
    path('setup/reauthenticate', views.reauthenticate),
    path('setup/logout', views.logout),
]
