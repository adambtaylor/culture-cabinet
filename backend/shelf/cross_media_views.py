"""Authenticated display of fixed offline examples, independent of user taste."""
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import render
from django.views.decorators.http import require_GET

from shelf.cross_media import SAMPLE_IDS, sample_context


@login_required
@require_GET
def sample(request):
    selectors = request.GET.getlist('trail')
    trail = selectors[0] if selectors else 'dune'
    if len(selectors) > 1 or trail not in SAMPLE_IDS:
        raise Http404('Unknown cross-media sample selector.')
    return render(request, 'cabinet/cross_media_sample.html', sample_context(trail))
