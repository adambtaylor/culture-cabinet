"""Optional postcode context stays local to selection and display."""
from dataclasses import asdict, dataclass
from functools import lru_cache
import re
import uuid

from . import grant_authority as ga
from .disclosure import canonical_bytes
from .enhance_contract import digest
from .postal_locality import PostalIndex, LocalReference, ATTRIBUTION, APPROXIMATION_NOTICE


@lru_cache(maxsize=1)
def postal_index():
    # One pinned public bundle load, never a geocoder or an owner-data request.
    return PostalIndex.load()


def validate_setting(country, postcode, radius_km=None):
    ga._require(country=='AU' and type(postcode) is str,'CONTRACT_INVALID',422)
    postcode=postcode.strip()
    ga._require(re.fullmatch('[0-9]{4}',postcode) is not None,'CONTRACT_INVALID',422)
    if radius_km is None:radius_km=25
    ga._require(type(radius_km) is int and 1<=radius_km<=500,'CONTRACT_INVALID',422)
    return country,postcode,radius_km


@dataclass(frozen=True)
class LocalEventContext:
    profile_ref: str
    location_revision: int
    dataset_revision: str | None
    reference: LocalReference | None
    radius_km: int | None
    setting_digest: str
    state: str

    def __post_init__(self):
        ga._require(str(ga._uuid(self.profile_ref))==self.profile_ref and
            type(self.location_revision) is int and self.location_revision>=0 and
            self.state in ('unset','mapped','unmapped','dataset_unavailable') and
            (self.reference is None or type(self.reference) is LocalReference) and
            (self.radius_km is None or type(self.radius_km) is int and 1<=self.radius_km<=500))
        ga._require((self.state=='mapped' and self.reference is not None and self.radius_km is not None)
            or (self.state=='unset' and self.reference is None and self.radius_km is None)
            or (self.state in ('unmapped','dataset_unavailable') and self.reference is None
                and self.radius_km is not None))

    @property
    def digest(self):
        material=asdict(self)
        if material['reference'] is not None:
            material['reference']['source_refs']=list(material['reference']['source_refs'])
        return digest(canonical_bytes({'schema':'local-event-context/v1',**material}))


def current_context(cabinet, *, index=None):
    from .models import PortableJourneyState
    row=PortableJourneyState.objects.filter(cabinet=cabinet).first()
    country=row.home_country if row else None
    postcode=row.home_postcode if row else None
    radius=row.radius_km if row else None
    revision=row.location_revision if row else 0
    index=postal_index() if index is None else index
    reference=None;state='unset';dataset_revision=getattr(index,'dataset_revision',None)
    if postcode is not None:
        validate_setting(country,postcode,radius)
        lookup=index.lookup(country,postcode)
        dataset_revision=lookup.dataset_revision;reference=lookup.reference
        state={'mapped':'mapped','unmapped':'unmapped','unavailable':'dataset_unavailable'}[lookup.state]
    setting_digest=digest(canonical_bytes({'country':country,'postcode':postcode,'radius_km':radius,
                                          'location_revision':revision}))
    return LocalEventContext(str(cabinet.profile_ref),revision,dataset_revision,reference,radius,setting_digest,state)


def location_projection(cabinet, *, journey_ref, expected_revision, enabled=True, index=None):
    from .models import PortableJourneyState
    from .portable_journey import form
    row=PortableJourneyState.objects.filter(cabinet=cabinet).first()
    context=current_context(cabinet,index=index)
    country=row.home_country if row else None
    postcode=row.home_postcode if row else None
    radius=row.radius_km if row else None
    common=dict(profile_ref=str(cabinet.profile_ref),journey_ref=journey_ref,expected_revision=expected_revision)
    def action(name,fields,available=True):
        return form('/cabinet/journey/location',{**common,'command_id':str(uuid.uuid4()),
            'action':name,**fields},enabled=enabled and available,
            reason='' if enabled else 'REAUTHENTICATION_REQUIRED')
    return dict(country=country,postcode=postcode,radius_km=radius,location_revision=context.location_revision,
        state=context.state,dataset_revision=context.dataset_revision,approximation_notice=APPROXIMATION_NOTICE,
        attribution=ATTRIBUTION,actions=dict(
            save=action('save',dict(country=country or 'AU',postcode=postcode or '',radius_km=radius or 25)),
            skip=action('skip',{}),remove=action('remove',{},postcode is not None)))
