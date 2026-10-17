"""Event rules shared by producers and the storage adapter; no orchestration."""
from copy import deepcopy
import json
from .schema_validation import validate


class EventRejected(ValueError):
    pass


def validate_event(event, trusted_source, trusted_actor=None):
    try:
        validate('RawEventV2', event)
    except Exception:
        raise EventRejected('EVENT_SCHEMA_INVALID') from None
    if event['source'] != trusted_source:
        raise EventRejected('EVENT_SOURCE_INVALID')
    actor = trusted_actor or {'api': 'user', 'ojWorker': 'system', 'toolExecutor': 'system', 'developerFixture': 'simulated'}.get(trusted_source)
    if event['actor'] != actor:
        raise EventRejected('EVENT_SOURCE_INVALID')
    if len(json.dumps(event, ensure_ascii=False).encode()) > 16384:
        raise EventRejected('EVENT_TOO_LARGE')


def business_content(event):
    """Delivery identity can change on retry; the operation and its occurrence cannot."""
    content = deepcopy(event)
    content.pop('eventId', None)
    return json.dumps(content, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def read_event(event):
    validate('RawEvent' if event.get('eventVersion') == 1 else 'RawEventV2', event)
    return deepcopy(event)
