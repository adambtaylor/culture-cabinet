"""Private beta reports with an immutable, server-resolved installation audience."""
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from . import recovery
from .models import BetaProblemReport, FeedbackTesterBinding, PortableInstallation


class ReportDenied(Exception):
    pass


class ReportConflict(Exception):
    pass


def bind_tester_account(*, tester_id, installation_id):
    """Trusted host setup seam; never called from a browser request."""
    User = get_user_model()
    with recovery.access_barrier(), transaction.atomic():
        tester = User.objects.select_for_update().filter(pk=tester_id, is_active=True).first()
        installation = PortableInstallation.objects.filter(pk=installation_id).first()
        if tester is None or installation is None:
            raise ReportDenied
        active = list(FeedbackTesterBinding.objects.select_for_update().filter(
            tester=tester, active=True))
        if len(active) > 1:
            raise ReportDenied
        if active and active[0].installation_id == installation.pk:
            return active[0]
        if active:
            active[0].active = False
            active[0].save(update_fields=['active'])
        return FeedbackTesterBinding.objects.create(tester=tester, installation=installation)


def submit_report(*, tester, reference, body):
    with recovery.access_barrier(), transaction.atomic():
        if not tester.is_active:
            raise ReportDenied
        links = list(FeedbackTesterBinding.objects.select_for_update().filter(
            tester=tester, active=True).select_related('installation'))
        if len(links) != 1:
            raise ReportDenied
        prior = BetaProblemReport.objects.filter(pk=reference).first()
        if prior:
            if prior.tester_id != tester.pk:
                raise ReportConflict
            if prior.deleted or prior.body is None or prior.body != body:
                raise ReportConflict
            return prior, False
        try:
            with transaction.atomic():
                report = BetaProblemReport.objects.create(reference=reference, tester=tester,
                    installation=links[0].installation, body=body)
        except IntegrityError:
            prior = BetaProblemReport.objects.filter(pk=reference, tester=tester).first()
            if prior is None or prior.deleted or prior.body != body:
                raise ReportConflict from None
            return prior, False
        return report, True


def tester_report(*, tester, reference):
    if not tester.is_active:
        raise ReportDenied
    with recovery.access_barrier():
        return BetaProblemReport.objects.filter(reference=reference, tester=tester,
            deleted=False, body__isnull=False).first()


def owner_report(*, authority, session_ref, reference):
    # The owner session and current process/database identity are rechecked for
    # every private read; the submission's installation snapshot never changes.
    from . import grant_authority as ga
    with ga.recovery_guard(authority, session_ref), authority._lock:
        installation, _ = ga._owner(authority, session_ref)
        return BetaProblemReport.objects.filter(reference=reference,
            installation=installation, deleted=False, body__isnull=False).first()
