"""Keep the exact reviewed descriptor private until its short confirmation expires.

Some indexers change article IDs on every retrieval. Comparing a second fetch is
correctly strict but can make those releases impossible to confirm. Persist the
actual reviewed bytes instead; never normalize IDs or declare two NZBs equal.
"""
from dataclasses import asdict
from datetime import timedelta

from django.utils import timezone

from shelf import obtain_acquisition as acquire
from shelf.acquisition_contract import Blocked
from shelf.models import ObtainReviewedNZB


class ReviewedRetriever(acquire.Retriever):
    def review(self, exact):
        descriptor = super().retrieve(exact)
        descriptor.sha256(exact)  # Enforce type, binding and the 1 MiB bound.
        now = timezone.now()
        # These are temporary descriptors, not acquisition/audit records. No
        # credentials or descriptor bytes enter tokens, HTML, logs or exports.
        ObtainReviewedNZB.objects.filter(expires_at__lte=now).delete()
        ObtainReviewedNZB.objects.update_or_create(request_id=exact.snapshot.request_id,
            defaults={'exact': asdict(exact), 'payload': descriptor.payload,
                      'expires_at': now + timedelta(minutes=5)})
        return descriptor

    def retrieve(self, exact):
        try:
            review = ObtainReviewedNZB.objects.get(request_id=exact.snapshot.request_id)
        except ObtainReviewedNZB.DoesNotExist:
            raise Blocked('Reviewed descriptor expired or unavailable; review again') from None
        if review.expires_at <= timezone.now() or review.exact != asdict(exact):
            raise Blocked('Reviewed descriptor expired or changed; review again')
        descriptor = acquire.RetrievedNZB(exact, bytes(review.payload))
        descriptor.sha256(exact)
        return descriptor
