"""Schema-aware authority for the one explicitly granted legacy household."""
from django.db import connection

from shelf.models import Grant, Profile


def legacy_owner_granted(actor_id, profile_id):
    if type(actor_id) is not int or type(profile_id) is not int:
        return False
    with connection.cursor() as cursor:
        columns = {column.name for column in
                   connection.introspection.get_table_description(cursor, Profile._meta.db_table)}
    profile = Profile.objects.filter(pk=profile_id, kind=Profile.Kind.OWNER)
    if 'owner_id' in columns:
        profile = profile.filter(owner__isnull=True)
    return profile.exists() and Grant.objects.filter(user_id=actor_id, profile_id=profile_id,
        can_view=True, can_select=True).exists()
