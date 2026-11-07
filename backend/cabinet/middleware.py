from django.conf import settings

class LocalSecurityMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response["Cache-Control"] = "no-store"
        response["X-Frame-Options"] = "DENY"
        local_scripts = "self" if settings.ROOT_URLCONF == "cabinet.provider_urls" else "none"
        response["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'self'; img-src 'self'; "
            f"font-src 'self'; script-src '{local_scripts}'; connect-src '{local_scripts}'; "
            "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
        )
        return response
