from django.http import HttpResponse


def unavailable(request, exception=None):
    return HttpResponse("Not available.\n", status=404, content_type="text/plain")


def bad_request(request, exception=None):
    return HttpResponse("Request rejected.\n", status=400, content_type="text/plain")


def forbidden(request, exception=None):
    return HttpResponse("Request rejected.\n", status=403, content_type="text/plain")


def csrf_failure(request, reason=""):
    return forbidden(request)


def server_error(request):
    return HttpResponse("Request unavailable.\n", status=500, content_type="text/plain")
