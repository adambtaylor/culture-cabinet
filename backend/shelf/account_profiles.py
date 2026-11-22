"""Authenticated account provisioning; never adopts an existing household slot."""
import sqlite3

from django.contrib.auth import get_user_model
from django.db import OperationalError
from django.http import Http404

from .feedback import Busy, immediate, is_busy
from .models import Grant, Profile


def provision_personal_profile(actor):
    if not actor.is_authenticated:
        raise Http404
    try:
        with immediate():
            if not get_user_model().objects.filter(pk=actor.pk, is_active=True).exists():
                raise Http404
            profile, created = Profile.objects.get_or_create(owner_id=actor.pk,
                defaults={'kind': 'personal', 'label': 'Your profile', 'active': True})
            if not profile.active:
                raise Http404
            # Profile and initial authority commit together. A missing grant on
            # an existing profile is indistinguishable from an explicit revoke.
            if created:
                Grant.objects.create(user_id=actor.pk, profile=profile,
                    can_view=True, can_select=True, can_mutate=True)
            return profile
    except (OperationalError, sqlite3.OperationalError) as error:
        if is_busy(error):
            raise Busy('Profile provisioning is busy. Reload and try again.') from None
        raise
