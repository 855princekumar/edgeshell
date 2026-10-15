"""What a person sees when something fails: a short, fixed description of the
kind of failure, never the exception's own text (which can carry paths, host
details or library internals). The full exception goes to the server log."""
import logging
import socket

log = logging.getLogger('edgeshell')


def describe(exc, context='', log_it=True):
    """A fixed message for `exc`, logged in full with `context` (log_it=False
    for callers that run every minute, such as the metrics collector)."""
    import paramiko
    from app.services.ssh_service import (LoginPaused, PAUSED_MESSAGE, TailscaleCheckRequired,
                                          TAILSCALE_CHECK_MESSAGE)
    if log_it:
        log.warning('%s failed', context or 'operation', exc_info=(type(exc), exc, exc.__traceback__))
    if isinstance(exc, TailscaleCheckRequired):
        return TAILSCALE_CHECK_MESSAGE
    if isinstance(exc, LoginPaused):
        return PAUSED_MESSAGE
    if isinstance(exc, paramiko.AuthenticationException):
        return 'Authentication failed: check the SSH username and password'
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return 'No answer (timed out)'
    if isinstance(exc, ConnectionRefusedError):
        return 'Connection refused'
    if isinstance(exc, socket.gaierror):
        return 'Unknown host name'
    if isinstance(exc, PermissionError):
        return 'Permission denied'
    if isinstance(exc, FileNotFoundError):
        return 'Not found'
    if isinstance(exc, paramiko.SSHException):
        return 'SSH error (details in the server log)'
    if isinstance(exc, OSError):
        return 'Network error (details in the server log)'
    return 'Unexpected error (details in the server log)'
