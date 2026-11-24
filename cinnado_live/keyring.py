"""Store one Wansview account in the desktop's Secret Service."""

import json

import gi


def _secret_service():
    gi.require_version('Secret', '1')
    from gi.repository import Secret

    schema = Secret.Schema.new('io.github.wansviewubuntu.CinnadoLive', Secret.SchemaFlags.NONE,
                               {'account': Secret.SchemaAttributeType.STRING})
    return Secret, schema, {'account': 'wansview-cloud'}


def load_account():
    secret, schema, attributes = _secret_service()
    value = secret.password_lookup_sync(schema, attributes, None)
    if value is None:
        return None
    values = json.loads(value)
    if not isinstance(values, list) or len(values) != 2 or not all(isinstance(item, str) and item for item in values):
        raise ValueError('Invalid saved account')
    return tuple(values)


def save_account(email, password):
    secret, schema, attributes = _secret_service()
    if not secret.password_store_sync(schema, attributes, secret.COLLECTION_DEFAULT,
                                      'B6ing · compte Wansview Cloud', json.dumps([email, password]), None):
        raise RuntimeError('Secret Service did not store the account')


def forget_account():
    secret, schema, attributes = _secret_service()
    secret.password_clear_sync(schema, attributes, None)
