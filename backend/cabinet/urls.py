from django.urls import path
from cabinet.assets import local_asset
from shelf import views
from shelf import obtain_views
from shelf import library_views

obtain_urlpatterns = [
    path('profiles/<int:profile_id>/acquired-files/', library_views.index, name='acquired_files'),
    path('profiles/<int:profile_id>/acquired-files/<int:job_id>/handoff/', library_views.handoff, name='acquired_files_handoff'),
    path('profiles/<int:profile_id>/saved/jobs/<int:job_id>/submit/review/', obtain_views.submission_review, name='obtain_submission_review'),
    path('profiles/<int:profile_id>/saved/jobs/<int:job_id>/submit/', obtain_views.submit, name='obtain_submit'),
    path('profiles/<int:profile_id>/saved/jobs/<int:job_id>/local-handoff/', obtain_views.local_handoff, name='obtain_local_handoff'),
]

urlpatterns = [
    path("login/", views.DemoLoginView.as_view(), name="login"),
    path("logout/", views.DemoLogoutView.as_view(), name="logout"),
    path("", views.catalogue, name="catalogue"),
    path("profiles/", views.profiles, name="profiles"),
    path("profiles/select/", views.select_profile, name="select_profile"),
    path("profiles/<int:profile_id>/", views.profile_catalogue, name="profile_catalogue"),
    path('profiles/<int:profile_id>/feedback/<uuid:target_id>/', views.feedback_detail, name='feedback_detail'),
    path('profiles/<int:profile_id>/feedback/<uuid:target_id>/mutate/', views.feedback_mutate, name='feedback_mutate'),
    path('profiles/<int:profile_id>/feedback-export/', views.feedback_export, name='feedback_export'),
    path("static/<path:path>", local_asset, name="local_asset"),
]
urlpatterns += obtain_urlpatterns
handler400 = "cabinet.errors.bad_request"
handler403 = "cabinet.errors.forbidden"
handler404 = "cabinet.errors.unavailable"
handler500 = "cabinet.errors.server_error"
