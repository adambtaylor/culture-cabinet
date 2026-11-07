"""Bound stalled clients in the single-threaded catalogue development server."""
from django.core.servers.basehttp import WSGIServer


class CatalogueWSGIServer(WSGIServer):
    def get_request(self):
        client, address = super().get_request()
        try:
            client.settimeout(2.0)
        except BaseException:
            client.close()
            raise
        return client, address
