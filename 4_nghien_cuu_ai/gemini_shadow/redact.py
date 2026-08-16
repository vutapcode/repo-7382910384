"""Secret redaction applied before prompts, logs, and datasets."""

import re


SENSITIVE_MARKERS = ('key', 'secret', 'token', 'password', 'private', 'credential')
TOKEN_PATTERN = re.compile(r'(?i)(api[_ -]?key|secret|token|password)\s*[:=]\s*[^\s,;}]+')


def redact(value, secrets=()):
    if isinstance(value, dict):
        return {
            str(key): (
                '[REDACTED]' if any(marker in str(key).lower() for marker in SENSITIVE_MARKERS)
                else redact(item, secrets)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, tuple):
        return [redact(item, secrets) for item in value]
    if isinstance(value, str):
        result = value
        for secret in secrets:
            if secret:
                result = result.replace(secret, '[REDACTED]')
        return TOKEN_PATTERN.sub(lambda match: match.group(1) + '=[REDACTED]', result)
    return value
