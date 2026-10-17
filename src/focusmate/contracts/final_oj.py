"""Shared final verification errors and canonical identities; no orchestration."""
import hashlib
import json


class FinalOJError(ValueError):
    pass


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


FINAL_STATUSES = {'verified', 'alreadyCompleted', 'requestFailed', 'superseded'}
