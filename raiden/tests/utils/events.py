# -*- coding: utf8 -*-
from raiden.api.python import RaidenAPI

NOVALUE = object()


def check_nested_attrs(item, data):
    for name, value in data.items():
        item_value = getattr(item, name, NOVALUE)

        if isinstance(value, dict):
            if not check_nested_attrs(item_value, value):
                return False

        elif item_value != value:
            return False

    return True


def must_contain_entry(item_list, type_, data):
    """ A node might have duplicated state changes or code changes may change
    order / quantity of the events.

    The number of state changes is non-deterministic since it depends on the
    number of retries from the transport layer.

    This is completely non-deterministic since the transport retries depend on
    timeouts and the cooperative scheduling of the running greenlets.
    Additionally the order / quantity of greenlet switches will change as the
    code evolves.

    This utility checks the list of state changes for an entry of the correct
    type with the expected data, ignoring *new* fields, repeated entries, and
    unexpected entries.
    """
    # item_list may be composed of state changes or events
    for item in item_list:
        if isinstance(item, type_):
            if check_nested_attrs(item, data):
                return item
    return None


def check_dict_nested_attrs(item, dict_data):
    for key, value in dict_data.items():
        if key not in item:
            return False

        item_value = item[key]

        if isinstance(item_value, dict):
            if not check_dict_nested_attrs(item_value, value):
                return False
        elif item_value != value:
            return False

    return True


def must_have_event(event_list, dict_data):
    for item in event_list:
        if isinstance(item, dict) and check_dict_nested_attrs(item, dict_data):
            return item
    return None


def get_channel_events_for_token(app, registry_address, token_address, start_block=0):
    """ Collect all events from all channels for a given `token_address` and `app` """
    result = list()
    api = RaidenAPI(app.raiden)
    channels = api.get_channel_list(
        registry_address=registry_address,
        token_address=token_address,
    )
    for channel in channels:
        events = api.get_channel_events(channel.identifier, start_block)
        result.extend(events)
    return result
