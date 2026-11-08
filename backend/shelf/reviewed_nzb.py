"""Keep the exact reviewed descriptor private until its short confirmation expires.

Some indexers change article IDs on every retrieval. Comparing a second fetch is
correctly strict but can make those releases impossible to confirm. Persist the
actual reviewed bytes instead; never normalize IDs or declare two NZBs equal.
"""
from datetime import datetime, timedelta

from django.utils import timezone

from shelf import obtain_acquisition as acquire
from shelf.acquisition_contract import Blocked
from shelf.models import ObtainReviewedNZB
from shelf.portable_saved_authority import active_portable_access, write_for


class ReviewedRetriever(acquire.Retriever):
    def review(self, exact):
        descriptor = super().retrieve(exact)
        descriptor.sha256(exact)  # Enforce type, binding and the 1 MiB bound.
        access = active_portable_access()
        now = access.authority.now() if access is not None else timezone.now()
        # These are temporary descriptors, not acquisition/audit records. No
        # credentials or descriptor bytes enter tokens, HTML, logs or exports.
        with write_for(actor=_actor(exact), profile_id=exact.snapshot.profile_id):
            # A reviewed descriptor conveys no permission to prune another
            # profile's rows, including native rows seen by a legacy caller.
            from shelf.models import ObtainPreview
            expired = ObtainReviewedNZB.objects.filter(expires_at__lte=now,
                request_id__in=ObtainPreview.objects.filter(actor_id=exact.snapshot.actor_id,
                    profile_id=exact.snapshot.profile_id).values('pk'))
            expired.delete()
            end = now + timedelta(minutes=5)
            if type(exact) is acquire.OriginExactResult:
                end = min(end, datetime.fromisoformat(exact.publication_origin['valid_until'].replace('Z', '+00:00')))
            ObtainReviewedNZB.objects.update_or_create(request_id=exact.snapshot.request_id,
                defaults={'exact': exact.material(), 'payload': descriptor.payload, 'expires_at': end})
        return descriptor

    def retrieve(self, exact):
        try:
            review = ObtainReviewedNZB.objects.get(request_id=exact.snapshot.request_id)
        except ObtainReviewedNZB.DoesNotExist:
            raise Blocked('Reviewed descriptor expired or unavailable; review again') from None
        access = active_portable_access()
        now = access.authority.now() if access is not None else timezone.now()
        if review.expires_at <= now or review.exact != exact.material():
            raise Blocked('Reviewed descriptor expired or changed; review again')
        descriptor = acquire.RetrievedNZB(exact, bytes(review.payload))
        descriptor.sha256(exact)
        return descriptor


def _actor(exact):
    from django.contrib.auth import get_user_model
    return get_user_model().objects.get(pk=exact.snapshot.actor_id)
