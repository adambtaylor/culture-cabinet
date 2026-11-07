"""Fixed provider-only surface. No legacy feedback/export/import/recovery URLs."""
from django.urls import path
from cabinet.assets import local_asset
from cabinet.urls import obtain_urlpatterns
from shelf import provider_views as views
from shelf import saved_views, obtain_views
from shelf import discovery_views, recommendation_views, event_plan_views, social_views, message_views, date_night_views, outing_views, real_outing_views, taste_views
from shelf import nonfiction_views, taste_observation_views, enhancement_views
from shelf.cross_media_views import sample
from shelf.views import DemoLoginView

urlpatterns = [
    path('profiles/<int:profile_id>/enhance/<str:category>/', enhancement_views.request_pick, name='enhancement_request'),
    path('profiles/<int:profile_id>/enhance/<str:category>/<uuid:request_id>/', enhancement_views.status, name='enhancement_status'),
    path('profiles/<int:profile_id>/enhance/<str:category>/<uuid:request_id>/apply/', enhancement_views.apply, name='enhancement_apply'),
    path('profiles/<int:profile_id>/enhance/<str:category>/<uuid:request_id>/cancel/', enhancement_views.cancel, name='enhancement_cancel'),
    path('profiles/<int:profile_id>/taste/observations/', taste_observation_views.index, name='taste_observations'),
    path('profiles/<int:profile_id>/taste/observations/review/', taste_observation_views.review, name='taste_observations_review'),
    path('profiles/<int:profile_id>/taste/observations/import/', taste_observation_views.confirm, name='taste_observations_import'),
    path('profiles/<int:profile_id>/nonfiction/', nonfiction_views.index, name='nonfiction_discovery'),
    path('profiles/<int:profile_id>/nonfiction/action/', nonfiction_views.action, name='nonfiction_action'),
    path('profiles/<int:profile_id>/nonfiction/export/', nonfiction_views.export, name='nonfiction_export'),
    path('profiles/<int:profile_id>/taste/', taste_views.index, name='taste_funnel'),
    path('profiles/<int:profile_id>/taste/action/', taste_views.action, name='taste_action'),
    path('profiles/<int:profile_id>/taste/fetch/', taste_views.fetch_round, name='taste_fetch'),
    path('profiles/<int:profile_id>/taste/synergize/', taste_views.synergize, name='taste_synergize'),
    path('profiles/<int:profile_id>/date-night/', date_night_views.detail, name='date_night'),
    path('profiles/<int:profile_id>/social-discovery/', social_views.index, name='social_discovery'),
    path('profiles/<int:profile_id>/social-discovery/action/', social_views.action, name='social_discovery_action'),
    path('profiles/<int:profile_id>/messages/', message_views.index, name='messages'),
    path('profiles/<int:profile_id>/messages/context/<slug:chat_id>/', message_views.selected_context, name='message_context'),
    path('profiles/<int:profile_id>/messages/start/', message_views.start, name='message_start'),
    path('profiles/<int:profile_id>/messages/<uuid:draft_id>/', message_views.detail, name='message_draft'),
    path('profiles/<int:profile_id>/messages/<uuid:draft_id>/action/', message_views.action, name='message_action'),
    path('profiles/<int:profile_id>/event-plans/', event_plan_views.index, name='event_plans'),
    path('profiles/<int:profile_id>/event-plans/start/', event_plan_views.start, name='event_plan_start'),
    path('profiles/<int:profile_id>/event-plans/<uuid:plan_id>/', event_plan_views.detail, name='event_plan'),
    path('profiles/<int:profile_id>/event-plans/<uuid:plan_id>/action/', event_plan_views.action, name='event_plan_action'),
    path('profiles/<int:profile_id>/event-plans/<uuid:plan_id>/restaurant/', event_plan_views.restaurant_action, name='restaurant_action'),
    path('profiles/<int:profile_id>/event-plans/<uuid:plan_id>/real-action/', real_outing_views.action, name='real_outing_action'),
    path('profiles/<int:profile_id>/event-plans/<uuid:plan_id>/outing/', outing_views.configure, name='outing_configure'),
    path('recommendations/', recommendation_views.home, name='recommendations'),
    path('profiles/<int:profile_id>/recommendations/', recommendation_views.index, name='recommendations_profile'),
    path('discovery/', discovery_views.home, name='discovery'),
    path('profiles/<int:profile_id>/discovery/', discovery_views.index, name='discovery_profile'),
    path('profiles/<int:profile_id>/discovery/action/', discovery_views.action, name='discovery_action'),
    path('samples/cross-media/', sample, name='cross_media_sample'),
    path('login/', DemoLoginView.as_view(template_name='registration/provider_login.html'), name='login'),
    path('logout/', views.ProviderLogoutView.as_view(), name='logout'),
    path('', views.catalogue_home, name='catalogue'),
    path('profiles/', views.profiles, name='profiles'),
    path('profiles/select/', views.select_profile, name='select_profile'),
    path('profiles/<int:profile_id>/', views.profile_catalogue, name='profile_catalogue'),
    path('profiles/<int:profile_id>/catalogue/action/', views.action, name='provider_action'),
    path('profiles/<int:profile_id>/intake/', views.intake_home, name='durable_intake'),
    path('profiles/<int:profile_id>/intake/action/', views.durable_action, name='durable_intake_action'),
    path('profiles/<int:profile_id>/saved/', saved_views.index, name='saved_items'),
    path('profiles/<int:profile_id>/saved/save/', saved_views.save, name='save_item'),
    path('profiles/<int:profile_id>/saved/<int:item_id>/remove/', saved_views.remove, name='remove_saved_item'),
    path('profiles/<int:profile_id>/saved/<int:item_id>/obtain/', saved_views.review, name='obtain_review'),
    path('profiles/<int:profile_id>/saved/<int:item_id>/obtain/confirm/', saved_views.confirm, name='obtain_confirm'),
    path('profiles/<int:profile_id>/saved/jobs/<int:job_id>/', saved_views.status, name='acquisition_status'),
    path('profiles/<int:profile_id>/saved/jobs/<int:job_id>/cancel/', saved_views.cancel, name='acquisition_cancel'),
    path('profiles/<int:profile_id>/saved/<int:item_id>/preview/', obtain_views.start, name='obtain_preview_start'),
    path('profiles/<int:profile_id>/saved/<int:item_id>/preview/search/', obtain_views.search, name='obtain_preview_search'),
    path('profiles/<int:profile_id>/saved/previews/<uuid:preview_id>/', obtain_views.results, name='obtain_preview_results'),
    path('profiles/<int:profile_id>/saved/previews/<uuid:preview_id>/results/<uuid:result_id>/', obtain_views.review, name='obtain_preview_review'),
    path('profiles/<int:profile_id>/saved/previews/<uuid:preview_id>/results/<uuid:result_id>/confirm/', obtain_views.confirm, name='obtain_preview_confirm'),
    path('static/<path:path>', local_asset, name='local_asset'),
]
urlpatterns += obtain_urlpatterns
handler400 = 'cabinet.errors.bad_request'
handler403 = 'cabinet.errors.forbidden'
handler404 = 'cabinet.errors.unavailable'
handler500 = 'cabinet.errors.server_error'
