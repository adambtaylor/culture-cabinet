"""Serve M's local assets through the same security middleware as every page."""
from pathlib import Path

from django.http import Http404
from django.views.decorators.http import require_GET
from django.views.static import serve

from cabinet.boundary import ROOT


@require_GET
def local_asset(request, path):
    asset_root = ROOT / "static"
    target = asset_root / path
    if (Path(path).is_absolute() or any(part in {".", ".."} for part in path.split("/"))
            or not target.resolve().is_relative_to(asset_root)):
        raise Http404
    return serve(request, path, document_root=asset_root)
