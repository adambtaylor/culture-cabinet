from django.conf import settings
from django.db import models
from django.utils import timezone


class Profile(models.Model):
    class Kind(models.TextChoices):
        OWNER = "owner", "Owner"
        PARTNER = "partner", "Partner"
        FAMILY = "family", "Family"

    kind = models.CharField(max_length=7, choices=Kind.choices, unique=True)
    label = models.CharField(max_length=80)
    active = models.BooleanField(default=False)

    class Meta:
        constraints = [models.CheckConstraint(
            condition=models.Q(kind__in=["owner", "partner", "family"]),
            name="profile_kind_allowed",
        )]
        ordering = ["id"]


class Grant(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    profile = models.ForeignKey(Profile, on_delete=models.CASCADE)
    can_view = models.BooleanField(default=False)
    can_select = models.BooleanField(default=False)
    can_mutate = models.BooleanField(default=False)
    can_export = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["user", "profile"], name="one_grant_per_user_profile",
        )]


class FeedbackTarget(models.Model):
    id = models.UUIDField(primary_key=True)
    kind = models.CharField(max_length=7, choices=[('item', 'Item'), ('session', 'Session')])
    parent = models.ForeignKey('self', null=True, on_delete=models.PROTECT)
    title = models.CharField(max_length=200)
    category = models.CharField(max_length=80)
    source_namespace = models.CharField(max_length=80)
    source_key = models.CharField(max_length=160)
    deleted = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['source_namespace', 'source_key'], name='feedback_source_identity'),
            models.CheckConstraint(condition=(models.Q(kind='item', parent__isnull=True) | models.Q(kind='session', parent__isnull=False)), name='feedback_target_shape'),
        ]


class FeedbackSubject(models.Model):
    profile = models.OneToOneField(Profile, on_delete=models.PROTECT)
    subject_id = models.UUIDField(unique=True)


class FeedbackEvent(models.Model):
    id = models.UUIDField(primary_key=True)
    profile = models.ForeignKey(Profile, on_delete=models.PROTECT)
    target = models.ForeignKey(FeedbackTarget, on_delete=models.PROTECT)
    signal = models.CharField(max_length=24)
    scope = models.CharField(max_length=7)
    revision = models.PositiveIntegerField()
    operation = models.CharField(max_length=7)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    authority_grant_id = models.BigIntegerField()
    recorded_at = models.DateTimeField()
    source_record_id = models.CharField(max_length=160, null=True)
    source_time = models.DateTimeField(null=True)
    origin = models.CharField(max_length=24)
    original_origin = models.CharField(max_length=24)
    attribution = models.JSONField(null=True)
    original_recorded_at = models.DateTimeField(null=True)
    source_metadata = models.JSONField(null=True)
    reverses = models.ForeignKey('self', null=True, on_delete=models.PROTECT, related_name='reversals')
    corrects = models.ForeignKey('self', null=True, on_delete=models.PROTECT, related_name='corrections')
    body = models.JSONField(null=True)
    erased = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['profile', 'target', 'signal', 'scope', 'revision'], name='feedback_event_revision'),
            models.UniqueConstraint(fields=['reverses'], condition=models.Q(reverses__isnull=False), name='feedback_one_reverse'),
            models.CheckConstraint(condition=models.Q(revision__gte=1), name='feedback_positive_revision'),
            models.CheckConstraint(condition=models.Q(operation__in=['set', 'reverse', 'erase']), name='feedback_operation'),
        ]


class FeedbackProjection(models.Model):
    profile = models.ForeignKey(Profile, on_delete=models.PROTECT)
    target = models.ForeignKey(FeedbackTarget, on_delete=models.PROTECT)
    signal = models.CharField(max_length=24)
    scope = models.CharField(max_length=7)
    revision = models.PositiveIntegerField(default=0)
    effective_event_id = models.UUIDField(null=True)
    value = models.JSONField(null=True)
    erased = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['profile', 'target', 'signal', 'scope'], name='feedback_projection_key')]


class MutationReceipt(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    mutation_id = models.UUIDField()
    profile_id = models.BigIntegerField()
    operation_family = models.CharField(max_length=24)
    target_id = models.UUIDField(null=True)
    payload_sha256 = models.CharField(max_length=64, null=True)
    event_id = models.UUIDField(null=True)
    accepted_revision = models.PositiveIntegerField(null=True)
    state = models.CharField(max_length=8, default='accepted')
    created_at = models.DateTimeField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=['actor', 'mutation_id'], name='feedback_actor_mutation')]


class RecoveryAnchor(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    store_id = models.UUIDField(unique=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(id=1), name='recovery_singleton')]


class RecoveryRestriction(models.Model):
    sequence = models.PositiveIntegerField(primary_key=True)
    kind = models.CharField(max_length=20)
    profile_id = models.BigIntegerField(null=True)
    target_id = models.UUIDField(null=True)
    signal = models.CharField(max_length=24, null=True)
    scope = models.CharField(max_length=7, null=True)
    subject_id = models.CharField(max_length=80, null=True)
    restriction_code = models.CharField(max_length=24)
    operation_uuid = models.UUIDField()
    local_commit_uuid = models.UUIDField()
    external_head_id = models.UUIDField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=['operation_uuid', 'sequence'], name='recovery_operation_sequence')]


class SavedItem(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    profile = models.ForeignKey(Profile, on_delete=models.PROTECT)
    provider = models.CharField(max_length=16)
    medium = models.CharField(max_length=5)
    source_id = models.PositiveIntegerField()
    list_kind = models.CharField(max_length=9)
    title = models.CharField(max_length=300)
    url = models.URLField(max_length=300)
    fetched_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    publication_lineage = models.JSONField(null=True, default=None)

    class Meta:
        ordering = ['id']
        constraints = [
            models.UniqueConstraint(fields=['actor', 'profile', 'provider', 'medium', 'source_id', 'list_kind'], name='saved_identity_list'),
            models.CheckConstraint(condition=(models.Q(provider='tmdb', medium__in=['movie', 'tv']) | models.Q(provider='hardcover', medium='book')), name='saved_provider_medium'),
            models.CheckConstraint(condition=(models.Q(list_kind='favorites') | models.Q(list_kind='watch', medium__in=['movie', 'tv']) | models.Q(list_kind='read', medium='book')), name='saved_list_medium'),
            models.CheckConstraint(condition=models.Q(source_id__gte=1, source_id__lte=2147483647), name='saved_source_id'),
        ]


class AcquisitionJob(models.Model):
    """Local request snapshot. This package has no enqueue or transport state."""
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    profile = models.ForeignKey(Profile, on_delete=models.PROTECT)
    saved_item = models.ForeignKey(SavedItem, null=True, on_delete=models.SET_NULL)
    request_id = models.UUIDField()
    active_key = models.CharField(max_length=64, null=True, unique=True)
    publication_origin = models.JSONField(null=True, default=None)
    provider = models.CharField(max_length=16, null=True)
    medium = models.CharField(max_length=5, null=True)
    source_id = models.PositiveIntegerField(null=True)
    title = models.CharField(max_length=300, null=True)
    scope = models.CharField(max_length=16)
    edition = models.CharField(max_length=200)
    storage = models.CharField(max_length=24)
    authorized_content = models.BooleanField()
    storage_check = models.BooleanField()
    status = models.CharField(max_length=24, default='blocked_configuration')
    reason = models.CharField(max_length=300)
    created_at = models.DateTimeField(auto_now_add=True)
    cancelled_at = models.DateTimeField(null=True)

    class Meta:
        ordering = ['id']
        constraints = [
            models.UniqueConstraint(fields=['actor', 'profile', 'request_id'], name='acquisition_request_receipt'),
            # The shared Python parser enforces canonical episode numbers. Keep
            # this family check usable by raw SQLite backup/restore connections.
            models.CheckConstraint(condition=(
                models.Q(publication_origin__isnull=False, scope='series_s01e01')
                | models.Q(publication_origin__isnull=False, scope__startswith='episode_s')
                | models.Q(provider='tmdb', medium='movie', scope='film')
                | (models.Q(provider='tmdb', medium='tv')
                   & (models.Q(scope='series_s01e01') | models.Q(scope__startswith='episode_s')))
                | models.Q(provider='hardcover', medium='book', scope='audiobook')
                | models.Q(provider='album_request', medium='music', scope='album_flac',
                           saved_item__isnull=True)), name='acquisition_exact_scope'),
            models.CheckConstraint(condition=models.Q(authorized_content=True, storage_check=True, storage='local_library'), name='acquisition_confirmed_checks'),
            models.CheckConstraint(condition=(models.Q(status='blocked_configuration', active_key__isnull=False, cancelled_at__isnull=True) | models.Q(status='cancelled', active_key__isnull=True, cancelled_at__isnull=False)), name='acquisition_local_state'),
            models.CheckConstraint(condition=(
                models.Q(publication_origin__isnull=True, provider__isnull=False,
                    medium__isnull=False, source_id__isnull=False, title__isnull=False)
                | models.Q(publication_origin__isnull=False, saved_item__isnull=True,
                    provider__isnull=True, medium__isnull=True, source_id__isnull=True,
                    title__isnull=True)), name='acquisition_origin_variant'),
        ]


# Intake additions are isolated from the existing model definitions.
import uuid


class IntakeRun(models.Model):
    """Only human intake and minimal judged-source provenance, never a candidate cache."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    profile = models.ForeignKey(Profile, on_delete=models.PROTECT)
    revision = models.PositiveIntegerField(default=0)
    nonce = models.CharField(max_length=64)
    selector_version = models.CharField(max_length=32, default='intake-v2.1')
    history = models.JSONField(default=list)
    receipts = models.JSONField(default=dict)
    imported_session = models.CharField(max_length=64, null=True, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    archived_at = models.DateTimeField(null=True)

    class Meta:
        ordering = ['-created_at', '-id']


class ObtainPreview(models.Model):
    """Immutable saved identity and short-lived local search receipt; no private payload."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    profile = models.ForeignKey(Profile, on_delete=models.PROTECT)
    publication_origin = models.JSONField(null=True, default=None)
    saved_identity = models.PositiveBigIntegerField(null=True)
    provider = models.CharField(max_length=16, null=True)
    medium = models.CharField(max_length=5, null=True)
    source_id = models.PositiveIntegerField(null=True)
    title = models.CharField(max_length=300, null=True)
    scope = models.CharField(max_length=16)
    nonce = models.UUIDField(default=uuid.uuid4, editable=False)
    version = models.PositiveIntegerField(default=1)
    state = models.CharField(max_length=24, default='searching')
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()

    class Meta:
        constraints = [models.CheckConstraint(condition=(
            models.Q(publication_origin__isnull=True, saved_identity__isnull=False,
                provider__isnull=False, medium__isnull=False, source_id__isnull=False,
                title__isnull=False)
            | models.Q(publication_origin__isnull=False, saved_identity__isnull=True,
                provider__isnull=True, medium__isnull=True, source_id__isnull=True,
                title__isnull=True)), name='obtain_preview_origin_variant')]


class ObtainResult(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    preview = models.ForeignKey(ObtainPreview, on_delete=models.CASCADE, related_name='results')
    guid = models.CharField(max_length=20)
    metadata = models.JSONField()
    digest = models.CharField(max_length=64)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['preview', 'guid'], name='obtain_preview_guid')]


class ObtainSelection(models.Model):
    """Exact local choice; legacy jobs have no binding or preview dependency."""
    job = models.OneToOneField(AcquisitionJob, primary_key=True, on_delete=models.PROTECT)
    result = models.ForeignKey(ObtainResult, on_delete=models.PROTECT)


class DiscoveryState(models.Model):
    """Minimal music/events human history; fixture and household modes are disjoint."""
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    profile = models.ForeignKey(Profile, on_delete=models.PROTECT)
    mode = models.CharField(max_length=9, choices=[('household', 'Household'), ('sample', 'Sample')])
    revision = models.PositiveIntegerField(default=0)
    nonce = models.CharField(max_length=200)
    history = models.JSONField(default=list)
    receipts = models.JSONField(default=dict)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['actor', 'profile', 'mode'], name='discovery_actor_profile_mode'),
            models.CheckConstraint(condition=models.Q(mode__in=['household', 'sample']), name='discovery_mode_allowed'),
        ]


class ObtainTransfer(models.Model):
    """Exact submission receipt and compare-and-swap dispatch checkpoint."""
    job = models.OneToOneField(AcquisitionJob, primary_key=True, on_delete=models.PROTECT)
    binding = models.JSONField()
    result_guid = models.CharField(max_length=20, unique=True)
    nzb_sha256 = models.CharField(max_length=64, unique=True)
    revision = models.PositiveIntegerField(default=0)
    state = models.CharField(max_length=24, default='blocked')
    reason = models.CharField(max_length=64, default='verification_required')
    provider_job = models.CharField(max_length=120, blank=True, default='')
    local_cancelled_at = models.DateTimeField(null=True)
    native_progress = models.JSONField(default=dict)
    library_verification = models.JSONField(null=True, default=None)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(state__in=[
            'blocked', 'outcome_unknown', 'queued', 'failed', 'cancel_pending',
            'cancelled', 'local_verified']), name='obtain_transfer_state')]


class ObtainLocalFile(models.Model):
    """Verified in-place file receipt, not an indexed library or playback record."""
    transfer = models.OneToOneField(ObtainTransfer, primary_key=True, on_delete=models.PROTECT)
    path = models.CharField(max_length=4096, unique=True)
    sha256 = models.CharField(max_length=64, unique=True)
    size = models.PositiveBigIntegerField()
    verified_at = models.DateTimeField(auto_now_add=True)
    acquired_at = models.DateTimeField(null=True)
    playback_verified = models.BooleanField(default=False)
    metadata = models.JSONField(default=dict)


class AlbumRequest(models.Model):
    """Explicit release mapping; its integer key is local, never a MusicBrainz ID."""
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    profile = models.ForeignKey(Profile, on_delete=models.PROTECT)
    request_id = models.UUIDField(unique=True)
    release_key = models.CharField(max_length=240)
    identity = models.JSONField()
    authorization = models.JSONField()
    authorized_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['actor', 'profile', 'release_key'],
                                               name='album_request_release')]


class PostprocessReceipt(models.Model):
    """Hook evidence, deliberately separate from final LocalCompletion."""
    transfer = models.OneToOneField(ObtainTransfer, primary_key=True, on_delete=models.PROTECT)
    receipt = models.JSONField()
    state = models.CharField(max_length=24, default='postprocess_verified')
    checked_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(state__in=[
            'postprocess_verified', 'unavailable']), name='postprocess_receipt_state')]


class PostprocessFile(models.Model):
    receipt = models.ForeignKey(PostprocessReceipt, on_delete=models.PROTECT, related_name='files')
    relative_path = models.CharField(max_length=4096)
    sha256 = models.CharField(max_length=64)
    size = models.PositiveBigIntegerField()
    verified_at = models.DateTimeField()

    class Meta:
        ordering = ['relative_path']
        constraints = [models.UniqueConstraint(fields=['receipt', 'relative_path'],
                                               name='postprocess_file_path')]


class ObtainCompletionStatus(models.Model):
    """Historical final-download and operator import facts, never live readiness."""
    transfer = models.OneToOneField(ObtainTransfer, primary_key=True, on_delete=models.PROTECT)
    completion = models.JSONField()
    completion_sha256 = models.CharField(max_length=64)
    observed_at = models.DateTimeField(default=timezone.now)
    delivery_state = models.CharField(max_length=16, default='not_reported')
    delivery_receipt = models.JSONField(default=dict)
    delivery_sha256 = models.CharField(max_length=64, blank=True, default='')
    delivery_at = models.DateTimeField(null=True)
    revision = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(delivery_state__in=[
            'not_reported', 'blocked', 'unknown', 'imported']), name='obtain_completion_delivery_state')]


class EventPlan(models.Model):
    """Private event intent and existing handoff contracts; complete-state CAS."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    profile = models.ForeignKey(Profile, on_delete=models.PROTECT)
    mode = models.CharField(max_length=12, default='sample')
    active_key = models.CharField(max_length=64, unique=True)
    version = models.PositiveIntegerField(default=0)
    nonce = models.CharField(max_length=64)
    data = models.JSONField(default=dict)
    receipts = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [models.CheckConstraint(condition=models.Q(mode__in=['sample', 'household']),
                                               name='event_plan_mode')]


class SocialDiscoveryState(models.Model):
    """Bounded selected-source queue and reviewed leads; no account/session material."""
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    profile = models.ForeignKey(Profile, on_delete=models.PROTECT)
    mode = models.CharField(max_length=12)
    revision = models.PositiveIntegerField(default=0)
    nonce = models.CharField(max_length=200)
    data = models.JSONField(default=dict)
    receipts = models.JSONField(default=dict)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['actor', 'profile', 'mode'], name='social_actor_profile_mode'),
            models.CheckConstraint(condition=models.Q(mode__in=['household', 'sample']), name='social_mode_allowed'),
        ]


class MessageDraft(models.Model):
    """One private rehearsal intent, immutable dispatch identity and durable evidence."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    profile = models.ForeignKey(Profile, on_delete=models.PROTECT)
    mode = models.CharField(max_length=12, default='sample')
    active_key = models.CharField(max_length=64, unique=True)
    state = models.CharField(max_length=12, default='draft')
    version = models.PositiveIntegerField(default=0)
    nonce = models.CharField(max_length=64)
    data = models.JSONField(default=dict)
    receipts = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.CheckConstraint(condition=models.Q(mode='sample'), name='message_sample_only'),
            models.CheckConstraint(condition=models.Q(state__in=[
                'draft', 'queued', 'sending', 'sent', 'failed', 'unknown']), name='message_state'),
        ]


class ObtainReviewedNZB(models.Model):
    """Temporary private exact bytes, separate from durable transfer receipts."""
    request = models.OneToOneField(ObtainPreview, primary_key=True,
                                   on_delete=models.CASCADE)
    exact = models.JSONField()
    payload = models.BinaryField()
    expires_at = models.DateTimeField()


class SynthesisRun(models.Model):
    """One explicit taste snapshot request and supervised inference receipt."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    profile = models.ForeignKey(Profile, on_delete=models.PROTECT)
    mode = models.CharField(max_length=9, default='household')
    request_identity = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    input_snapshot = models.JSONField()
    input_digest = models.CharField(max_length=64)
    state = models.CharField(max_length=10, default='queued')
    revision = models.PositiveIntegerField(default=0)
    attempts = models.PositiveSmallIntegerField(default=0)
    output = models.JSONField(default=dict)
    evidence = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    contract_version = models.CharField(max_length=20, default='legacy/v1')
    installation = models.ForeignKey('PortableInstallation', null=True, on_delete=models.PROTECT)
    portable_connection = models.ForeignKey('PortableConnection', null=True, on_delete=models.PROTECT)
    client_key = models.UUIDField(null=True)
    refresh_nonce = models.UUIDField(null=True)
    portable_stage = models.CharField(max_length=24, default='')
    portable_bindings = models.JSONField(default=dict)
    collection_digest = models.CharField(max_length=64, default='')
    body_digest = models.CharField(max_length=64, default='')
    sealed_run_digest = models.CharField(max_length=64, default='')
    transport_request_id = models.UUIDField(null=True, unique=True)
    portable_expires_at = models.DateTimeField(null=True)
    cancel_requested = models.BooleanField(default=False)
    content_redacted_at = models.DateTimeField(null=True)
    content_digest = models.CharField(max_length=64, default='')

    class Meta:
        ordering = ['-created_at', '-id']
        constraints = [
            models.UniqueConstraint(fields=['actor', 'profile', 'input_digest'], condition=models.Q(contract_version='legacy/v1'), name='synthesis_actor_profile_input'),
            models.CheckConstraint(condition=(models.Q(contract_version='legacy/v1', mode='household', installation__isnull=True, client_key__isnull=True)
                | models.Q(contract_version='enhance/v1', mode='portable', installation__isnull=False,
                           portable_connection__isnull=False, client_key__isnull=False, refresh_nonce__isnull=False)), name='synthesis_household_only'),
            models.UniqueConstraint(fields=['installation', 'actor', 'profile', 'client_key'], condition=models.Q(contract_version='enhance/v1'), name='enhance_client_identity'),
            models.UniqueConstraint(fields=['installation', 'actor', 'profile', 'input_digest'], condition=models.Q(contract_version='enhance/v1'), name='enhance_input_identity'),
            models.UniqueConstraint(fields=['profile'], condition=models.Q(contract_version='enhance/v1', attempts=1, state__in=['running', 'unknown']), name='enhance_unresolved_profile'),
            models.CheckConstraint(condition=models.Q(state__in=[
                'queued', 'running', 'completed', 'failed', 'unknown']), name='synthesis_state'),
            models.CheckConstraint(condition=models.Q(attempts__lte=1), name='synthesis_one_attempt'),
        ]


class AuthoredDiscoveryState(models.Model):
    """Explicit topic seeds and episode shortlist, independent of taste ratings."""
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    profile = models.ForeignKey(Profile, on_delete=models.PROTECT)
    mode = models.CharField(max_length=12)
    revision = models.PositiveIntegerField(default=0)
    nonce = models.CharField(max_length=200)
    data = models.JSONField(default=dict)
    receipts = models.JSONField(default=dict)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['actor', 'profile', 'mode'], name='authored_actor_profile_mode'),
            models.CheckConstraint(condition=models.Q(mode__in=['household', 'sample']), name='authored_mode_allowed'),
        ]


class PortableInstallation(models.Model):
    """Nonsecret portable ownership; legacy Profile/Grant authority is separate."""
    id = models.UUIDField(primary_key=True, serialize=False)
    data_schema_version = models.PositiveIntegerField(default=1)
    root_identity = models.CharField(max_length=64, unique=True)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)
    owner_revision = models.PositiveBigIntegerField(default=0)
    process_generation = models.UUIDField()
    failed_auth = models.PositiveSmallIntegerField(default=0)
    cooldown_until = models.DateTimeField(null=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=(models.Q(owner__isnull=True, owner_revision=0)
                | models.Q(owner__isnull=False, owner_revision__gte=1)), name='portable_owner_revision'),
            models.CheckConstraint(condition=models.Q(failed_auth__lte=5), name='portable_auth_failure_cap'),
        ]


class PortableConnection(models.Model):
    """Unproven nonsecret configuration, never a working-inference receipt."""
    id = models.UUIDField(primary_key=True, serialize=False)
    installation = models.ForeignKey(PortableInstallation, on_delete=models.PROTECT)
    adapter = models.CharField(max_length=32)
    model = models.CharField(max_length=128)
    reasoning = models.CharField(max_length=16)
    config_revision = models.PositiveBigIntegerField(default=1)
    credential_generation = models.PositiveBigIntegerField(default=0)
    validation_generation = models.PositiveBigIntegerField(default=0)
    removed = models.BooleanField(default=False)


class SavedCredentialBinding(models.Model):
    """Nonsecret intent/CAS state; encrypted bytes belong only to Secret Service."""
    connection = models.OneToOneField(PortableConnection, primary_key=True, on_delete=models.PROTECT)
    binding = models.JSONField()
    config_revision = models.PositiveBigIntegerField()
    state = models.CharField(max_length=16)
    generation = models.UUIDField()
    operation_id = models.UUIDField()
    store_revision = models.CharField(max_length=128, null=True)
    entered_at = models.DateTimeField()
    updated_at = models.DateTimeField()
    expires_at = models.DateTimeField(null=True)
    scopes = models.JSONField(default=list)
    removed = models.BooleanField(default=False)
    remove_requested = models.BooleanField(default=False)
    delete_no_effect_proof = models.JSONField(null=True, default=None)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(
            state__in=['PENDING_WRITE', 'ACTIVE', 'PENDING_DELETE', 'UNAVAILABLE']),
            name='saved_credential_binding_state')]


class PortableProbeAttempt(models.Model):
    """One neutral connection check, not a recommendation broker or prompt store."""
    id = models.UUIDField(primary_key=True, serialize=False)
    connection = models.ForeignKey(PortableConnection, on_delete=models.PROTECT)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    preview_ref = models.UUIDField()
    bindings = models.JSONField()
    body_digest = models.CharField(max_length=64)
    state = models.CharField(max_length=12)
    outcome = models.CharField(max_length=32)
    created_at = models.DateTimeField()

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(
            state__in=['prepared', 'sending', 'completed', 'failed', 'unknown']), name='portable_probe_state')]


class PortableValidation(models.Model):
    """Minimal proof metadata; no provider response, prompt or authentication."""
    attempt = models.OneToOneField(PortableProbeAttempt, primary_key=True, serialize=False, on_delete=models.PROTECT)
    bindings = models.JSONField()
    proof_kind = models.CharField(max_length=32)
    observed_model = models.CharField(max_length=128, null=True)
    tested_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    observation_limits = models.JSONField(default=list)


class ProfileCabinetState(models.Model):
    """Portable identity/floors and one atomic, revisioned publication pointer."""
    profile = models.OneToOneField(Profile, primary_key=True, serialize=False, on_delete=models.PROTECT)
    installation = models.ForeignKey(PortableInstallation, on_delete=models.PROTECT)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    profile_ref = models.UUIDField(default=uuid.uuid4, unique=True)
    actor_ref = models.UUIDField(default=uuid.uuid4, unique=True)
    subject_ref = models.UUIDField(default=uuid.uuid4, unique=True)
    owner_revision = models.PositiveBigIntegerField()
    active_manifest_ref = models.UUIDField(null=True)
    publication_revision = models.PositiveBigIntegerField(default=0)
    policy_epoch = models.PositiveBigIntegerField(default=0)
    restriction_watermark = models.PositiveBigIntegerField(default=0)
    data_schema_version = models.PositiveIntegerField(default=1)
    blocked = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(owner_revision__gte=1), name='cabinet_owner_revision'),
            models.CheckConstraint(condition=(models.Q(active_manifest_ref__isnull=True, publication_revision=0)
                | models.Q(active_manifest_ref__isnull=False, publication_revision__gte=1)), name='cabinet_publication_dormant'),
            models.CheckConstraint(condition=models.Q(data_schema_version__in=[1, 2]), name='cabinet_schema_v1'),
        ]


class PortableDisclosureGrant(models.Model):
    """One retained consent identity; revocation is a tombstone, never deletion."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, serialize=False)
    cabinet = models.ForeignKey(ProfileCabinetState, on_delete=models.PROTECT)
    connection = models.ForeignKey(PortableConnection, on_delete=models.PROTECT)
    purpose = models.CharField(max_length=16)
    config_revision = models.PositiveBigIntegerField()
    config_digest = models.CharField(max_length=64)
    allowed_data_classes = models.JSONField(default=list)
    revision = models.PositiveBigIntegerField(default=1)
    issued_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    state = models.CharField(max_length=7, default='active')

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['cabinet', 'connection', 'purpose'], name='disclosure_grant_scope'),
            models.CheckConstraint(condition=models.Q(purpose__in=['inference', 'catalogue_lookup']), name='disclosure_grant_purpose'),
            models.CheckConstraint(condition=models.Q(state__in=['active', 'revoked']), name='disclosure_grant_state'),
            models.CheckConstraint(condition=models.Q(revision__gte=1), name='disclosure_grant_revision'),
            models.CheckConstraint(condition=models.Q(expires_at__gt=models.F('issued_at')), name='disclosure_grant_expiry'),
        ]


class PortableDisclosurePreview(models.Model):
    """Opaque refs/digests only; no persisted prompt, note, credential or send state."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, serialize=False)
    cabinet = models.ForeignKey(ProfileCabinetState, on_delete=models.PROTECT)
    connection = models.ForeignKey(PortableConnection, on_delete=models.PROTECT)
    input_refs = models.JSONField()
    bindings = models.JSONField()
    serializer_version = models.CharField(max_length=40)
    body_digest = models.CharField(max_length=64)
    omitted_groups = models.JSONField(default=list)
    created_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    state = models.CharField(max_length=7, default='preview')

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(state__in=['preview', 'stale']), name='disclosure_preview_state'),
            models.CheckConstraint(condition=models.Q(expires_at__gt=models.F('created_at')), name='disclosure_preview_expiry'),
        ]


class PortableCabinetManifest(models.Model):
    """Immutable permitted publication; references a run, never another broker."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, serialize=False)
    cabinet = models.ForeignKey(ProfileCabinetState, on_delete=models.PROTECT)
    run = models.OneToOneField(SynthesisRun, on_delete=models.PROTECT)
    publication_revision = models.PositiveBigIntegerField()
    bindings = models.JSONField()
    manifest = models.JSONField()
    created_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    content_redacted_at = models.DateTimeField(null=True)
    content_digest = models.CharField(max_length=64, default='')

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['cabinet', 'publication_revision'], name='cabinet_manifest_revision'),
            models.CheckConstraint(condition=models.Q(publication_revision__gte=1), name='cabinet_manifest_positive'),
            models.CheckConstraint(condition=models.Q(expires_at__gt=models.F('created_at')), name='cabinet_manifest_expiry'),
        ]


def _closed_enhance_write_guard(sender, connection, **kwargs):
    # A shared legacy broker now has portable-only triggers. Register DENIAL on
    # Django connections so merely compiling those triggers cannot break legacy
    # writes. Only the existing connection-local service window can replace 0.
    if connection.vendor == 'sqlite':
        register_closed_sql_guards(connection.connection)


def register_closed_sql_guards(raw):
    """Initialize Django AND participating raw handles without granting a write.

    SQLite resolves trigger functions even when a legacy row misses its WHEN.
    Only the existing scoped transaction may temporarily replace this zero.
    """
    raw.create_function('cabinet_disclosure_write', 0, lambda: 0)
    raw.create_function('cabinet_content_digest', -1, _content_digest)
    raw.create_function('cabinet_redacted_selection', 1, _redacted_selection_sql)
    raw.create_function('cabinet_redacted_selection_valid', 2, _redacted_selection_valid_sql)
    raw.create_function('cabinet_selection_valid', 7, _selection_valid_sql)
    raw.create_function('cabinet_selection_field_equal', 3, _selection_field_equal_sql)
    raw.create_function('cabinet_saved_lineage_valid', 1, _saved_lineage_valid)
    raw.create_function('cabinet_obtain_origin_valid', 1, _obtain_origin_valid)
    raw.create_function('cabinet_obtain_scope_valid', 1, _obtain_scope_valid)
    raw.create_function('cabinet_obtain_write', 2, lambda actor, profile: 0)



def _selection_json(raw):
    from .disclosure import read_json
    if type(raw) is not str:raise ValueError('Closed JSON required')
    return read_json(raw.encode('utf-8'))


def _redacted_selection_sql(raw):
    from .enhance_selection import redacted_selection_evidence
    from .disclosure import canonical_bytes
    return canonical_bytes(redacted_selection_evidence(_selection_json(raw))).decode('utf-8')


def _redacted_selection_valid_sql(old,new):
    from .enhance_selection import redacted_selection_evidence
    try:return int(_selection_json(new)==redacted_selection_evidence(_selection_json(old)))
    except (ValueError,TypeError,KeyError):return 0


def _selection_valid_sql(evidence,record,refresh,input_digest,run_ref,collection,redacted):
    from .enhance_selection import validate_selection_history
    try:
        if type(redacted) is not int or redacted not in (0,1):return 0
        validate_selection_history(_selection_json(record),_selection_json(evidence),input_digest=input_digest,
            refresh_nonce=refresh,run_ref=run_ref,collection_digest=collection,redacted=bool(redacted))
        return 1
    except (ValueError,TypeError,KeyError):return 0


def _selection_field_equal_sql(old,new,key):
    try:
        if key not in ('selection_seed','selection_policy_digest','selection_report'):return 0
        old,new=_selection_json(old),_selection_json(new)
        return int((key in old)==(key in new) and old.get(key)==new.get(key))
    except (ValueError,TypeError,KeyError):return 0


def _obtain_origin_valid(raw):
    from .disclosure import read_json
    from .portable_obtain_origin import validate_obtain_origin
    try:
        if type(raw) is not str or len(raw.encode('utf-8')) > 1536:
            return 0
        validate_obtain_origin(read_json(raw.encode('utf-8')))
        return 1
    except (ValueError, TypeError, AttributeError, UnicodeError, RecursionError):
        return 0


def _obtain_scope_valid(raw):
    from .episode_scope import parse_scope, ScopeError
    try:
        return int(type(raw) is str and parse_scope(raw) is not None)
    except ScopeError:
        return 0


def validate_publication_lineage(value):
    """Bounded opaque attribution only; display permission is not retention rights."""
    import json
    import re
    from uuid import UUID
    fields = {'schema', 'installation_ref', 'owner_revision', 'profile_ref',
              'publication_ref', 'publication_revision', 'run_ref', 'candidate_key',
              'candidate_digest', 'source_evidence_digest'}
    if type(value) is not dict or set(value) != fields or value['schema'] != 'portable-saved-lineage/v1':
        raise ValueError('Invalid publication lineage')
    for key in ('installation_ref', 'profile_ref', 'publication_ref', 'run_ref'):
        if type(value[key]) is not str or str(UUID(value[key])) != value[key] or UUID(value[key]).int == 0:
            raise ValueError('Invalid publication lineage')
    for key in ('owner_revision', 'publication_revision'):
        if type(value[key]) is not int or not 1 <= value[key] <= 2**53 - 1:
            raise ValueError('Invalid publication lineage')
    for key in ('candidate_digest', 'source_evidence_digest'):
        if type(value[key]) is not str or not re.fullmatch('[0-9a-f]{64}', value[key]):
            raise ValueError('Invalid publication lineage')
    if (type(value['candidate_key']) is not str or not 1 <= len(value['candidate_key']) <= 300
            or not value['candidate_key'].strip()):
        raise ValueError('Invalid publication lineage')
    if len(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()) > 2048:
        raise ValueError('Invalid publication lineage')
    return value


def _saved_lineage_valid(raw):
    # SQLite owns validation/immutability too: authorized raw/bulk writers cannot
    # manufacture malformed or replace surviving lineage through an ORM bypass.
    import json
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result
    try:
        if type(raw) is not str or len(raw.encode('utf-8')) > 2048:
            return 0
        validate_publication_lineage(json.loads(raw, object_pairs_hook=unique))
        return 1
    except (ValueError, TypeError, AttributeError, UnicodeError, RecursionError):
        return 0


def _content_digest(*parts):
    # The guard verifies the exact pre-redaction content with the existing codec;
    # a supplied checksum alone is never permission to discard/refill a payload.
    import hashlib
    from .disclosure import canonical_bytes, read_json
    try:
        if len(parts) not in (1, 2):
            return None
        # Each original payload keeps the canonical codec's 256KiB bound. The
        # LOCAL digest of two payloads must not inherit a single wire-body cap.
        encoded = [canonical_bytes(read_json(part.encode())) for part in parts]
        return hashlib.sha256(b'[' + b','.join(encoded) + b']').hexdigest()
    except (ValueError, TypeError, AttributeError):
        return None


from django.db.backends.signals import connection_created
connection_created.connect(_closed_enhance_write_guard, weak=False,
                           dispatch_uid='cabinet.closed_enhance_write_guard')


class PortableJourneyState(models.Model):
    """Resumable nonsecret progress; preferences remain in typed feedback."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, serialize=False)
    cabinet = models.OneToOneField(ProfileCabinetState, on_delete=models.PROTECT)
    revision = models.PositiveBigIntegerField(default=1)
    step = models.CharField(max_length=16, default='providers')
    completed = models.JSONField(default=list)
    skipped = models.JSONField(default=list)
    slots = models.JSONField(default=dict)
    connections = models.JSONField(default=dict)
    run_ref = models.UUIDField(null=True)
    home_country = models.CharField(max_length=2, null=True)
    home_postcode = models.CharField(max_length=4, null=True)
    radius_km = models.PositiveSmallIntegerField(null=True)
    location_revision = models.PositiveBigIntegerField(default=0)
    next_noise = models.PositiveSmallIntegerField(default=25)
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.CheckConstraint(condition=(models.Q(home_country__isnull=True,
                home_postcode__isnull=True,radius_km__isnull=True) | models.Q(home_country='AU',
                home_postcode__isnull=False,radius_km__isnull=False,radius_km__gte=1,radius_km__lte=500)),name='journey_local_setting'),
            models.CheckConstraint(condition=models.Q(next_noise__lte=100),name='journey_noise_range'),
        ]


class PortableJourneyCommand(models.Model):
    """Replay/transition metadata, never another preference or source store."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, serialize=False)
    cabinet = models.ForeignKey(ProfileCabinetState, on_delete=models.PROTECT)
    command_id = models.UUIDField()
    kind = models.CharField(max_length=40)
    descriptor = models.JSONField()
    descriptor_digest = models.CharField(max_length=64)
    state = models.CharField(max_length=16)
    result = models.JSONField(default=dict)
    proof = models.JSONField(default=dict)
    created_at = models.DateTimeField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=['cabinet', 'command_id'],
                                               name='journey_owner_command')]


class PortableSourceCheckAttempt(models.Model):
    """One explicit fixed nonpersonal source check; history is not live proof."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, serialize=False)
    cabinet = models.ForeignKey(ProfileCabinetState, on_delete=models.PROTECT)
    connection = models.ForeignKey(PortableConnection, on_delete=models.PROTECT)
    command = models.OneToOneField(PortableJourneyCommand, on_delete=models.PROTECT)
    binding = models.JSONField()
    operation_digest = models.CharField(max_length=64)
    state = models.CharField(max_length=16)
    outcome = models.JSONField(default=dict)
    created_at = models.DateTimeField()
    expires_at = models.DateTimeField()
