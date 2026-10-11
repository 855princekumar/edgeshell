"""Backend-agnostic VPN status.

Dispatches on the VPN_BACKEND setting so the UI and heartbeat do not care
whether the mesh is Tailscale (today) or WireGuard (planned migration on the
same EC2 host).
"""
from flask import current_app
from app.services import tailscale_service
from app.services import wireguard_service


def get_status():
    backend = current_app.config.get('VPN_BACKEND', 'tailscale')
    if backend == 'wireguard':
        iface = current_app.config.get('WIREGUARD_INTERFACE', 'wg0')
        return wireguard_service.get_status(iface)
    status = tailscale_service.get_tailscale_status()
    status.setdefault('backend', 'tailscale')
    return status
