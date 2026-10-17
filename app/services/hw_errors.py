"""What a person sees when a hardware or webhook request cannot be carried
out: a fixed text looked up by a code, never an exception's own text (the
same rule as app/services/errors.py)."""

MESSAGES = {
    # categories
    'category_key': 'A category key may only use a-z, 0-9 and _.',
    'category_name': 'Give every category a name.',
    'too_many_categories': 'At most 30 categories.',
    # detected changes
    'decided': 'This change was already decided.',
    'decision': 'Decide confirm or reject.',
    'reason': 'Choose why the hardware was changed: fault, upgrade, maintenance or other.',
    'fault_pick': 'Pick an open fault of this node, or none.',
    # fault reports
    'started_format': '"started_at" must be a date and time, e.g. 2026-10-14T08:00:00Z.',
    'started_range': '"started_at" must be within the last year and not in the future.',
    'no_nodes': 'Choose at least one node.',
    'too_many_nodes': 'At most 500 nodes in one report.',
    'component': 'Choose a component from the list.',
    'symptom': 'Describe the symptom in a few words.',
    'severity': 'Severity must be one of: low, normal, high, critical.',
    'resolution': 'Resolution must be one of: replaced, repaired, reseated, no_fault_found, other.',
    'not_open': 'Only an open fault can be marked in progress.',
    'note': 'Write a note.',
    'resolved': 'This fault is already resolved.',
    'not_resolved': 'Only a resolved fault can be reopened.',
    'action': 'Unknown action.',
    # replacements
    'self': 'A node cannot replace itself.',
    'already_replaced': 'That node was already replaced.',
    'retired': 'A retired node cannot replace another node.',
    'replaces_other': 'This node already replaces another node.',
    'loop': 'That would make a loop.',
    'export': 'Export faults, components or changes.',
    # webhooks
    'url_long': 'The address is too long.',
    'url_https': 'Use an https:// address.',
    'url_http': 'Use an https:// address (plain http only for private networks, when allowed).',
    'url_creds': 'Put credentials in the receiver, not in the address.',
    'url_host': 'The address has no host.',
    'url_dns': 'The host name does not resolve.',
    'url_internal': 'That address points inside the server and is refused.',
    'url_private': 'Private network addresses are refused unless WEBHOOK_ALLOW_PRIVATE is set.',
    'hook_name': 'Give the webhook a name.',
    'hook_events': 'Choose at least one event.',
    'too_many_hooks': 'At most 20 webhooks.',
}
DEFAULT = 'That request is not valid.'


class HardwareError(ValueError):
    """A request that cannot be carried out. Carries a code, not a text."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def public_message(exc):
    """The fixed text for an error raised by the hardware or webhook services."""
    return MESSAGES.get(getattr(exc, 'code', None), DEFAULT)
