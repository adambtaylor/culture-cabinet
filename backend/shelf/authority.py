"""Selections are navigation; only current database grants confer authority."""
from django.http import Http404
from shelf.models import Profile
from django.contrib.auth import get_user_model

SELECTION_KEY = "selected_profile_id"


def permitted_profiles(actor, action):
    from shelf.recovery import access_barrier
    if action not in {'view', 'select', 'mutate', 'export'}:
        raise ValueError('Unknown authority action')
    if not actor.is_authenticated:
        return Profile.objects.none()
    if not get_user_model().objects.filter(pk=actor.pk, is_active=True).exists():
        return Profile.objects.none()
    with access_barrier() as barrier:
        from django.db import connection
        barrier.verify_mirror(connection.connection)
        filters = {'active': True, 'grant__user_id': actor.pk, 'grant__can_view': True}
        if action != 'view': filters['grant__can_' + action] = True
        ids = [p.pk for p in Profile.objects.filter(**filters)
               if not barrier.pending_denies(p.pk, action=action, actor_id=actor.pk)]
        # Materialize authority decisions while the barrier is held. Views hold
        # the enclosing barrier through rendering; services repeat in transaction.
        result = Profile.objects.filter(pk__in=ids)
        list(result)
        return result


def require_profile(actor, target, action):
    # Reject ambiguous, huge or non-numeric IDs before touching the query layer.
    if isinstance(target, bool) or not str(target).isascii() or not str(target).isdigit():
        raise Http404
    target = str(target)
    if len(target) > 18:
        raise Http404
    from shelf.recovery import access_barrier
    with access_barrier():
        profile = permitted_profiles(actor, action).filter(pk=int(target)).first()
        if profile is None:
            raise Http404
        return profile
