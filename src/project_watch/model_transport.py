"""Public-only HTTPS POST, DNS pinned, no redirects or ambient proxies."""

import http.client
import io
import ipaddress
import json
import socket
import ssl
import urllib.error
from urllib.parse import urlencode

from project_watch.feeds import GuardedHTTPS, _resolve_public


def _public_dns(host):
    """Fixed public DNS pair for fake-IP recovery, never model/private contents.

    Fail over on transport I/O only. HTTP rejection, invalid/private answers or
    certificate failures are terminal; another resolver cannot override them.
    """
    for server in ('1.1.1.1', '1.0.0.1'):
        connection = http.client.HTTPSConnection(server, timeout=5)
        try:
            connection.request(
                "GET",
                "/dns-query?" + urlencode({"name": host, "type": "A"}),
                headers={"Accept": "application/dns-json"},
            )
            response = connection.getresponse()
            raw = response.read(16385)
        except ssl.SSLCertVerificationError:
            raise
        except OSError as exc:
            if server == '1.0.0.1':
                raise urllib.error.URLError('public DNS unavailable') from exc
            continue
        finally:
            connection.close()
        if response.status != 200 or len(raw) > 16384:
            raise urllib.error.URLError("public DNS unavailable")
        break
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("Status") != 0:
            raise ValueError('invalid DNS status')
        addresses = [r["data"] for r in data.get("Answer", []) if r.get("type") == 1]
        if not addresses or any(not ipaddress.IPv4Address(ip).is_global for ip in addresses):
            raise ValueError('invalid public DNS addresses')
        return list(dict.fromkeys(addresses))
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise urllib.error.URLError('public DNS unavailable') from exc


class PublicModelOpener:
    def open(self, request, timeout=60):
        host, path = GuardedHTTPS._target(request.full_url)
        addresses = _resolve_public(host)
        if addresses and all(
            ipaddress.ip_address(ip) in ipaddress.ip_network("198.18.0.0/15") for ip in addresses
        ):
            addresses = _public_dns(host)
        if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
            raise urllib.error.URLError("disallowed model destination")
        class Pinned(http.client.HTTPSConnection):
            def connect(self):
                # Fail over only before ANY HTTP body is sent. Never replay a
                # model POST after a provider response or an ambiguous read error.
                for index, ip in enumerate(addresses[:3]):
                    raw = None
                    try:
                        raw = socket.create_connection((ip, 443), timeout=min(5, timeout))
                        self.sock = self._context.wrap_socket(raw, server_hostname=host)
                        self.sock.settimeout(timeout)
                        return
                    except ssl.SSLCertVerificationError:
                        if raw is not None:
                            raw.close()
                        raise
                    except OSError:
                        if raw is not None:
                            raw.close()
                        if index == len(addresses[:3])-1:
                            raise

        connection = Pinned(host, 443, timeout=timeout, context=ssl.create_default_context())
        try:
            connection.request(
                "POST", path, body=request.data, headers=dict(request.header_items())
            )
            response = connection.getresponse()
            if response.status != 200:
                raise urllib.error.HTTPError(
                    request.full_url, response.status, "model request rejected", {}, None
                )
            body = response.read(2_000_001)
            if len(body) > 2_000_000:
                raise urllib.error.URLError("model response too large")
            return io.BytesIO(body)
        finally:
            connection.close()
