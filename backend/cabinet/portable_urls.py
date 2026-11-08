"""Separate portable URLconf; legacy entrypoints keep their original routes."""
from django.urls import path
from shelf import portable_setup_views as views
from shelf import portable_connection_views as connections
from shelf import onboarding_experience_views as experience
from shelf import enhancement_views as enhance
from shelf import portable_obtain_views as obtain
from shelf import portable_source_views as sources
from shelf import portable_feedback_views as feedback

urlpatterns = [
    path('setup/cabinet', views.cabinet_entry),
    path('setup/cabinet/create', views.create_cabinet),
    path('setup/cabinet/bind', views.bind_cabinet),
    path('cabinet', enhance.portable_cabinet, name='portable_cabinet'),
    path('cabinet/obtain/status.js', obtain.status_script, name='portable_obtain_status_script'),
    path('cabinet/obtain/jobs/<int:job_id>/', obtain.job_page, name='portable_obtain_job'),
    path('cabinet/obtain/jobs/<int:job_id>/status/', obtain.job_status, name='portable_obtain_job_status'),
    path('cabinet/source/musicbrainz', sources.musicbrainz),
    path('cabinet/source/musicbrainz/configure', sources.musicbrainz_configure),
    path('cabinet/source/musicbrainz/consent', sources.musicbrainz_consent),
    path('cabinet/source/musicbrainz/artist', sources.musicbrainz_artist),
    path('cabinet/source/musicbrainz/artist/configure', sources.musicbrainz_artist_configure),
    path('cabinet/source/musicbrainz/artist/consent', sources.musicbrainz_artist_consent),
    path('cabinet/taste/artist', sources.artist),
    path('cabinet/taste', feedback.intake),
    path('cabinet/feedback', feedback.card_mutate),
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

# Native portable routes deliberately do not concatenate the provider URLconf.
urlpatterns += [
    path('cabinet/saved/', obtain.index, name='portable_saved'),
    path('cabinet/saved/select/', obtain.select, name='portable_saved_select'),
    path('cabinet/saved/save/', obtain.save, name='portable_saved_save'),
    path('cabinet/saved/<int:item_id>/remove/', obtain.remove, name='portable_saved_remove'),
    path('cabinet/saved/<int:item_id>/search/', obtain.search, name='portable_saved_search'),
    path('cabinet/saved/previews/<uuid:preview_ref>/', obtain.results, name='portable_obtain_results'),
    path('cabinet/saved/previews/<uuid:preview_ref>/<uuid:result_ref>/review/', obtain.review, name='portable_obtain_review'),
    path('cabinet/saved/previews/<uuid:preview_ref>/<uuid:result_ref>/confirm/', obtain.confirm, name='portable_obtain_confirm'),
    path('cabinet/saved/jobs/<int:job_id>/', obtain.status, name='portable_obtain_status'),
    path('cabinet/saved/jobs/<int:job_id>/submit/review/', obtain.submission_review, name='portable_obtain_submission_review'),
    path('cabinet/saved/jobs/<int:job_id>/submit/', obtain.submit, name='portable_obtain_submit'),
    path('cabinet/saved/jobs/<int:job_id>/cancel/', obtain.cancel, name='portable_obtain_cancel'),
]

# Fixed URL kwargs, never a browser-posted adapter or action URL.
for suffix, handler in (('', sources.source_page), ('/configure', sources.source_configure),
                        ('/consent', sources.source_consent)):
    urlpatterns.append(path('cabinet/source/apple/podcasts' + suffix, handler,
                            {'adapter': 'apple_podcast'}))

for adapter, page in (('hardcover', 'hardcover'), ('tmdb_movie', 'tmdb/movie'), ('tmdb_tv', 'tmdb/tv')):
    for suffix, handler in (('', sources.source_page), ('/configure', sources.source_configure),
                            ('/credential', sources.source_credential), ('/consent', sources.source_consent)):
        urlpatterns.append(path('cabinet/source/' + page + suffix, handler, {'adapter': adapter}))
