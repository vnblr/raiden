# -*- coding: utf-8 -*-
import gevent
import pytest
from raiden.transfer import views

from raiden.tests.utils.transfer import (
    assert_synched_channel_state,
    direct_transfer,
)


@pytest.mark.parametrize('number_of_nodes', [2])
def test_direct_transfer(raiden_network, token_addresses, deposit, network_wait):
    token_address = token_addresses[0]
    app0, app1 = raiden_network

    amount = 10
    node_state = views.state_from_app(app0)
    payment_network_id = app0.raiden.default_registry.address
    token_network_identifier = views.get_token_network_identifier_by_token_address(
        node_state,
        payment_network_id,
        token_address,
    )
    direct_transfer(
        app0,
        app1,
        token_network_identifier,
        amount,
        timeout=network_wait,
    )

    assert_synched_channel_state(
        token_network_identifier,
        app0, deposit - amount, [],
        app1, deposit + amount, [],
    )


@pytest.mark.parametrize('number_of_nodes', [2])
@pytest.mark.parametrize('channels_per_node', [1])
@pytest.mark.parametrize('number_of_tokens', [1])
def test_direct_transfer_to_offline_node(raiden_network, token_addresses, deposit):
    app0, app1 = raiden_network
    token_address = token_addresses[0]
    node_state = views.state_from_app(app0)
    payment_network_id = app0.raiden.default_registry.address
    token_network_identifier = views.get_token_network_identifier_by_token_address(
        node_state,
        payment_network_id,
        token_address,
    )

    # Wait until the initialization of the node is complete and then stop it
    gevent.wait([app1.raiden.start_event])
    app1.raiden.stop()

    amount = 10
    target = app1.raiden.address
    app0.raiden.direct_transfer_async(
        token_network_identifier,
        amount,
        target,
        identifier=1,
    )

    app1.raiden.start()

    gevent.sleep(5)

    no_outstanding_locks = []
    assert_synched_channel_state(
        token_network_identifier,
        app0, deposit - amount, no_outstanding_locks,
        app1, deposit + amount, no_outstanding_locks,
    )
