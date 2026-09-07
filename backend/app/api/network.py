"""Which addresses this machine can be reached on, and a QR code for them.

The point is to remove the "what is the IP?" step at an event: an organiser
opens this page on the PC running Reconize, and everyone else scans the code
with a phone on the same Wi-Fi.

The QR is returned INSIDE the authenticated JSON rather than as its own image
URL. An <img src="..."> request carries no Authorization header, so a separate
image endpoint would have to be public - and this one names the machine's
internal address, which is not something to hand out unauthenticated.
"""
from __future__ import annotations

import os
import socket
import time

from fastapi import APIRouter, Depends

from app.auth.deps import get_current_user
from app.models.models import User

router = APIRouter(prefix="/api/network", tags=["network"])

DEFAULT_PORT = 8000
_NETWORK_CACHE_TTL_SECONDS = 10
_network_cache: dict[int, tuple[float, dict]] = {}

# Addresses that exist on the machine but are useless to a phone: loopback,
# link-local (a failed DHCP), and the private ranges Hyper-V and VirtualBox
# create for their own virtual switches. Showing those sends people to an
# address that will never answer.
_VIRTUAL_PREFIXES = ("172.19.", "192.168.56.", "169.254.", "127.")


def _primary_ip() -> str | None:
    """The address this machine would use to reach the outside world.

    On a laptop with Wi-Fi plus a couple of virtual switches this is reliably
    the real one, because the routing table picks it. No packet is actually
    sent - connect() on a UDP socket only selects a route.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.settimeout(0.2)
        probe.connect(("8.8.8.8", 80))
        return probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()


def _all_ipv4() -> list[str]:
    found: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(info[4][0])
    except socket.gaierror:
        pass
    return sorted(found)


def _looks_usable(ip: str) -> bool:
    return not ip.startswith(_VIRTUAL_PREFIXES)


def _advertised_name() -> str | None:
    """The name Reconize publishes for itself, when it is actually advertised."""
    try:
        from app.services import mdns_service

        st = mdns_service.status()
        return st["hostname"] if st["advertised"] else None
    except Exception:  # noqa: BLE001
        return None


def _url(host: str, port: int) -> str:
    """Build the URL, leaving the port off when it is the scheme's default.

    run_server.py sets RECONIZE_SCHEME so this can build the RIGHT scheme -
    without it, a QR code generated while serving https-only on 443 encoded
    "http://host:443", which loads nothing: nothing on that machine is
    listening for plain http on 443, only the http->https redirector on port
    80 is (and only when 443 is the port actually chosen). getUserMedia's
    secure-context requirement (see cameras.ts) is the whole reason https is
    used at all, so a scheme this wrong silently defeated every remote
    camera use case §48 exists for.
    """
    scheme = os.environ.get("RECONIZE_SCHEME", "http")
    default_port = 443 if scheme == "https" else 80
    return f"{scheme}://{host}" if port == default_port else f"{scheme}://{host}:{port}"


def _mdns_name() -> str | None:
    """This machine's "<name>.local" address, if mDNS actually resolves it.

    Windows advertises its own hostname over mDNS, so this usually works with
    nothing installed. It is worth far more than the IP address for an event:
    the IP changes whenever the Wi-Fi does, and this name does not.

    It is verified by resolving it rather than assumed, because it depends on
    the network allowing multicast - and printing an address that does not
    answer is worse than printing none.
    """
    name = f"{socket.gethostname().lower()}.local"
    try:
        socket.gethostbyname(name)
        return name
    except OSError:
        return None


def addresses(port: int = DEFAULT_PORT) -> dict:
    primary = _primary_ip()
    others = [ip for ip in _all_ipv4() if _looks_usable(ip) and ip != primary]
    mdns = _mdns_name()
    advertised = _advertised_name()
    return {
        "hostname": socket.gethostname(),
        "port": port,
        "primary_url": _url(primary, port) if primary else None,
        # Survives a Wi-Fi change, unlike the IP. Resolves on iPhone, Mac and
        # Windows; Android's support for .local in the browser is patchy, which
        # is why the IP stays the one in the QR code.
        "mdns_url": _url(mdns, port) if mdns else None,
        # The name Reconize publishes for itself. Preferred over the machine
        # name because it does not change if the PC is renamed.
        "app_url": _url(advertised, port) if advertised else None,
        "other_urls": [_url(ip, port) for ip in others],
        # Kept separate and clearly labelled rather than hidden: if the primary
        # guess is wrong, the admin needs to see what else exists.
        "ignored": [ip for ip in _all_ipv4() if not _looks_usable(ip)],
    }


def qr_svg(url: str, scale: int = 6) -> str | None:
    """QR for a URL as an inline SVG string, or None if segno is unavailable."""
    try:
        import segno
    except ImportError:
        return None
    # Medium error correction: still scans if the screen is partly obscured or
    # photographed at an angle, without making the code much denser.
    code = segno.make(url, error="m")
    return code.svg_inline(scale=scale, dark="#1f2937", light=None)


@router.get("")
def network_info(port: int = DEFAULT_PORT, user: User = Depends(get_current_user)):
    cached = _network_cache.get(port)
    if cached and time.monotonic() - cached[0] < _NETWORK_CACHE_TTL_SECONDS:
        return cached[1]
    info = addresses(port)
    # The QR encodes the IP, not the .local name: every phone can open an IP,
    # while Android often cannot resolve .local. Because this is recomputed on
    # every request, changing Wi-Fi and reloading regenerates it automatically.
    info["qr_svg"] = qr_svg(info["primary_url"]) if info["primary_url"] else None
    info["mdns_qr_svg"] = qr_svg(info["mdns_url"]) if info["mdns_url"] else None
    info["app_qr_svg"] = qr_svg(info["app_url"]) if info["app_url"] else None
    # Deep links for the distributed camera-node flow (§45-47): the SAME
    # base address, pointed at a specific page instead of "/", so scanning
    # one QR both connects the device to Central AND opens the right screen -
    # a Windows PC wants the setup form, a phone/iPad wants Recognition.
    base = info["primary_url"]
    info["camera_node_url"] = f"{base}/camera-node" if base else None
    info["camera_node_qr_svg"] = qr_svg(info["camera_node_url"]) if info["camera_node_url"] else None
    info["cctv_url"] = f"{base}/cctv" if base else None
    info["cctv_qr_svg"] = qr_svg(info["cctv_url"]) if info["cctv_url"] else None
    # Phones/iPads never run a Local Agent (§27), so their deep link pins
    # central inference up front rather than making them pick it from the
    # setup form meant for a Windows station.
    info["mobile_recognition_url"] = f"{base}/camera-node?inference=central" if base else None
    info["mobile_recognition_qr_svg"] = qr_svg(info["mobile_recognition_url"]) if info["mobile_recognition_url"] else None
    # True only when the server is actually listening on every interface. Bound
    # to localhost, the address below would be correct and still unreachable,
    # which is the confusing case this flag exists to prevent.
    info["shared_on_network"] = _is_bound_to_all(port)
    _network_cache[port] = (time.monotonic(), info)
    return info


def _is_bound_to_all(port: int) -> bool:
    """Can something other than this PC reach us?

    Tested by connecting to our own LAN address rather than by inspecting
    configuration: that is the same path a phone takes, so it answers the
    question that actually matters.
    """
    ip = _primary_ip()
    if not ip:
        return False
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.4)
        probe.connect((ip, port))
        return True
    except OSError:
        return False
    finally:
        probe.close()
