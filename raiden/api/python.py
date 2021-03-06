# -*- coding: utf-8 -*-
import gevent
from contextlib import ExitStack
import structlog
from eth_utils import is_binary_address

from raiden import waiting
from raiden.blockchain.events import (
    ALL_EVENTS,
    get_all_registry_events,
    get_all_netting_channel_events,
    get_all_channel_manager_events,
)
from raiden.transfer import views
from raiden.transfer.events import (
    EventTransferSentSuccess,
    EventTransferSentFailed,
    EventTransferReceivedSuccess,
)
from raiden.transfer.state_change import ActionChannelClose
from raiden.exceptions import (
    AlreadyRegisteredTokenAddress,
    ChannelBusyError,
    ChannelNotFound,
    EthNodeCommunicationError,
    InsufficientFunds,
    InvalidAddress,
    InvalidAmount,
    InvalidSettleTimeout,
    UnknownTokenAddress,
)
from raiden.settings import (
    DEFAULT_POLL_TIMEOUT,
)
from raiden.utils import (
    pex,
    releasing,
)

log = structlog.get_logger(__name__)  # pylint: disable=invalid-name

EVENTS_EXTERNALLY_VISIBLE = (
    EventTransferSentSuccess,
    EventTransferSentFailed,
    EventTransferReceivedSuccess,
)


class RaidenAPI:
    # pylint: disable=too-many-public-methods

    def __init__(self, raiden):
        self.raiden = raiden

    @property
    def address(self):
        return self.raiden.address

    # XXX: This interface will break once the channel identifiers are not addresses
    def get_channel(self, registry_address, channel_address):
        if not is_binary_address(channel_address):
            raise InvalidAddress('Expected binary address format for channel in get_channel')

        channel_list = self.get_channel_list(registry_address)
        for channel in channel_list:
            if channel.identifier == channel_address:
                return channel

        raise ChannelNotFound()

    def token_network_register(
            self,
            registry_address,
            token_address,
            poll_timeout=DEFAULT_POLL_TIMEOUT,
    ):
        """Register the `token_address` in the blockchain. If the address is already
           registered but the event has not been processed this function will block
           until the next block to make sure the event is processed.

        Raises:
            InvalidAddress: If the registry_address or token_address is not a valid address.
            AlreadyRegisteredTokenAddress: If the token is already registered.
            TransactionThrew: If the register transaction failed, this may
                happen because the account has not enough balance to pay for the
                gas or this register call raced with another transaction and lost.
        """

        if not is_binary_address(registry_address):
            raise InvalidAddress('registry_address must be a valid address in binary')

        if not is_binary_address(token_address):
            raise InvalidAddress('token_address must be a valid address in binary')

        if token_address in self.get_tokens_list(registry_address):
            raise AlreadyRegisteredTokenAddress('Token already registered')

        try:
            registry = self.raiden.chain.registry(registry_address)
            return registry.add_token(token_address)
        finally:
            # Assume the transaction failed because the token is already
            # registered with the smart contract and this node has not yet
            # polled for the event (otherwise the check above would have
            # failed).
            #
            # To provide a consistent view to the user, wait one block, this
            # will guarantee that the events have been processed.
            next_block = self.raiden.get_block_number() + 1
            waiting.wait_for_block(self.raiden, next_block, poll_timeout)

    def token_network_connect(
            self,
            registry_address,
            token_address,
            funds,
            initial_channel_target=3,
            joinable_funds_target=.4,
    ):
        """Automatically maintain channels open for the given token network.

        Args:
            token_address (bin): the ERC20 token network to connect to.
            funds (int): the amount of funds that can be used by the ConnectionMananger.
            initial_channel_target (int): number of channels to open proactively.
            joinable_funds_target (float): fraction of the funds that will be used to join
                channels opened by other participants.
        """
        if not is_binary_address(token_address):
            raise InvalidAddress('token_address must be a valid address in binary')

        connection_manager = self.raiden.connection_manager_for_token(
            registry_address,
            token_address,
        )

        connection_manager.connect(
            funds,
            initial_channel_target=initial_channel_target,
            joinable_funds_target=joinable_funds_target,
        )

    def token_network_leave(self, registry_address, token_address, only_receiving=True):
        """Close all channels and wait for settlement."""
        if not is_binary_address(token_address):
            raise InvalidAddress('token_address must be a valid address in binary')

        if token_address not in self.get_tokens_list(registry_address):
            raise UnknownTokenAddress('token_address unknown')

        connection_manager = self.raiden.connection_manager_for_token(
            registry_address,
            token_address,
        )

        return connection_manager.leave(only_receiving)

    def channel_open(
            self,
            registry_address,
            token_address,
            partner_address,
            settle_timeout=None,
            reveal_timeout=None,
            poll_timeout=DEFAULT_POLL_TIMEOUT,
    ):
        """ Open a channel with the peer at `partner_address`
        with the given `token_address`.
        """
        if reveal_timeout is None:
            reveal_timeout = self.raiden.config['reveal_timeout']

        if settle_timeout is None:
            settle_timeout = self.raiden.config['settle_timeout']

        if settle_timeout <= reveal_timeout:
            raise InvalidSettleTimeout(
                'reveal_timeout can not be larger-or-equal to settle_timeout',
            )

        if not is_binary_address(registry_address):
            raise InvalidAddress('Expected binary address format for registry in channel open')

        if not is_binary_address(token_address):
            raise InvalidAddress('Expected binary address format for token in channel open')

        if not is_binary_address(partner_address):
            raise InvalidAddress('Expected binary address format for partner in channel open')

        registry = self.raiden.chain.registry(registry_address)
        channel_manager = registry.manager_by_token(token_address)
        netcontract_address = channel_manager.new_netting_channel(
            partner_address,
            settle_timeout,
        )

        msg = 'After {} seconds the channel was not properly created.'.format(
            poll_timeout,
        )

        with gevent.Timeout(poll_timeout, EthNodeCommunicationError(msg)):
            waiting.wait_for_newchannel(
                self.raiden,
                registry_address,
                token_address,
                partner_address,
                self.raiden.alarm.wait_time,
            )

        return netcontract_address

    def channel_deposit(
            self,
            registry_address,
            token_address,
            partner_address,
            amount,
            poll_timeout=DEFAULT_POLL_TIMEOUT,
    ):
        """ Deposit `amount` in the channel with the peer at `partner_address` and the
        given `token_address` in order to be able to do transfers.

        Raises:
            InvalidAddress: If either token_address or partner_address is not
            20 bytes long.
            TransactionThrew: May happen for multiple reasons:
                - If the token approval fails, e.g. the token may validate if
                  account has enough balance for the allowance.
                - The deposit failed, e.g. the allowance did not set the token
                  aside for use and the user spent it before deposit was called.
                - The channel was closed/settled between the allowance call and
                  the deposit call.
            AddressWithoutCode: The channel was settled during the deposit
            execution.
        """
        node_state = views.state_from_raiden(self.raiden)

        token_networks = views.get_token_network_addresses_for(
            node_state,
            registry_address,
        )
        channel_state = views.get_channelstate_for(
            node_state,
            registry_address,
            token_address,
            partner_address,
        )

        if not is_binary_address(token_address):
            raise InvalidAddress('Expected binary address format for token in channel deposit')

        if not is_binary_address(partner_address):
            raise InvalidAddress('Expected binary address format for partner in channel deposit')

        if token_address not in token_networks:
            raise UnknownTokenAddress('Unknown token address')

        if channel_state is None:
            raise InvalidAddress('No channel with partner_address for the given token')

        token = self.raiden.chain.token(token_address)
        balance = token.balance_of(self.raiden.address)

        # If this check succeeds it does not imply the the `deposit` will
        # succeed, since the `deposit` transaction may race with another
        # transaction.
        if not balance >= amount:
            msg = 'Not enough balance to deposit. {} Available={} Tried={}'.format(
                pex(token_address),
                balance,
                amount,
            )
            raise InsufficientFunds(msg)

        netcontract_address = channel_state.identifier
        channel_proxy = self.raiden.chain.netting_channel(netcontract_address)

        # If concurrent operations are happening on the channel, fail the request
        if not channel_proxy.channel_operations_lock.acquire(blocking=False):
            raise ChannelBusyError(
                f'Channel with id {channel_state.identifier} is '
                f'busy with another ongoing operation',
            )

        with releasing(channel_proxy.channel_operations_lock):
            token.approve(netcontract_address, amount)
            channel_proxy.deposit(amount)

            old_balance = channel_state.our_state.contract_balance
            target_balance = old_balance + amount

            msg = 'After {} seconds the deposit was not properly processed.'.format(
                poll_timeout,
            )

            # Wait until the `ChannelNewBalance` event is processed.
            with gevent.Timeout(poll_timeout, EthNodeCommunicationError(msg)):
                target_address = self.raiden.address
                waiting.wait_for_participant_newbalance(
                    self.raiden,
                    registry_address,
                    token_address,
                    partner_address,
                    target_address,
                    target_balance,
                    self.raiden.alarm.wait_time,
                )

    def channel_close(
            self,
            registry_address,
            token_address,
            partner_address,
            poll_timeout=DEFAULT_POLL_TIMEOUT,
    ):
        """Close a channel opened with `partner_address` for the given
        `token_address`.

        Race condition, this can fail if channel was closed externally.
        """
        self.channel_batch_close(
            registry_address,
            token_address,
            [partner_address],
            poll_timeout,
        )

    def channel_batch_close(
            self,
            registry_address,
            token_address,
            partner_addresses,
            poll_timeout=DEFAULT_POLL_TIMEOUT,
    ):
        """Close a channel opened with `partner_address` for the given
        `token_address`.

        Race condition, this can fail if channel was closed externally.
        """

        if not is_binary_address(token_address):
            raise InvalidAddress('Expected binary address format for token in channel close')

        if not all(map(is_binary_address, partner_addresses)):
            raise InvalidAddress('Expected binary address format for partner in channel close')

        valid_tokens = views.get_token_network_addresses_for(
            views.state_from_raiden(self.raiden),
            registry_address,
        )
        if token_address not in valid_tokens:
            raise UnknownTokenAddress('Token address is not known.')

        node_state = views.state_from_raiden(self.raiden)
        channels_to_close = views.filter_channels_by_partneraddress(
            node_state,
            registry_address,
            token_address,
            partner_addresses,
        )
        token_network_identifier = views.get_token_network_identifier_by_token_address(
            views.state_from_raiden(self.raiden),
            registry_address,
            token_address,
        )

        # If concurrent operations are happening on one of the channels, fail entire
        # request.
        with ExitStack() as stack:
            # Put all the locks in this outer context so that the netting channel functions
            # don't release the locks when their context goes out of scope
            for channel_state in channels_to_close:
                channel = self.raiden.chain.netting_channel(channel_state.identifier)

                # Check if we can acquire the lock. If we can't raise an exception, which
                # will cause the ExitStack to exit, releasing all locks acquired so far
                if not channel.channel_operations_lock.acquire(blocking=False):
                    raise ChannelBusyError(
                        f'Channel with id {channel_state.identifier} is '
                        f'busy with another ongoing operation.',
                    )

                stack.push(channel.channel_operations_lock)

            for channel_state in channels_to_close:
                channel_close = ActionChannelClose(
                    token_network_identifier,
                    channel_state.identifier,
                )

                self.raiden.handle_state_change(channel_close)

            msg = 'After {} seconds the closing transactions were not properly processed.'.format(
                poll_timeout,
            )

            channel_ids = [channel_state.identifier for channel_state in channels_to_close]

            with gevent.Timeout(poll_timeout, EthNodeCommunicationError(msg)):
                waiting.wait_for_close(
                    self.raiden,
                    registry_address,
                    token_address,
                    channel_ids,
                    self.raiden.alarm.wait_time,
                )

    def get_channel_list(self, registry_address, token_address=None, partner_address=None):
        """Returns a list of channels associated with the optionally given
           `token_address` and/or `partner_address`.

        Args:
            token_address (bin): an optionally provided token address
            partner_address (bin): an optionally provided partner address

        Return:
            A list containing all channels the node participates. Optionally
            filtered by a token address and/or partner address.

        Raises:
            KeyError: An error occurred when the token address is unknown to the node.
        """

        if token_address and not is_binary_address(token_address):
            raise InvalidAddress('Expected binary address format for token in get_channel_list')

        if partner_address and not is_binary_address(partner_address):
            raise InvalidAddress('Expected binary address format for partner in get_channel_list')

        result = list()
        if token_address and partner_address:
            channel_state = views.get_channelstate_for(
                views.state_from_raiden(self.raiden),
                registry_address,
                token_address,
                partner_address,
            )

            if channel_state:
                result = [channel_state]
            else:
                result = []

        elif token_address:
            result = views.list_channelstate_for_tokennetwork(
                views.state_from_raiden(self.raiden),
                registry_address,
                token_address,
            )

        elif partner_address:
            result = views.list_channelstate_for_tokennetwork(
                views.state_from_raiden(self.raiden),
                registry_address,
                partner_address,
            )

        else:
            result = views.list_all_channelstate(
                views.state_from_raiden(self.raiden),
            )

        return result

    def get_node_network_state(self, node_address):
        """ Returns the currently network status of `node_address`. """
        return views.get_node_network_status(
            views.state_from_raiden(self.raiden),
            node_address,
        )

    def start_health_check_for(self, node_address):
        """ Returns the currently network status of `node_address`. """
        self.raiden.start_health_check_for(node_address)

    def get_tokens_list(self, registry_address):
        """Returns a list of tokens the node knows about"""
        tokens_list = views.get_token_network_addresses_for(
            views.state_from_raiden(self.raiden),
            registry_address,
        )
        return tokens_list

    def transfer_and_wait(
            self,
            registry_address,
            token_address,
            amount,
            target,
            identifier=None,
            timeout=None):
        """ Do a transfer with `target` with the given `amount` of `token_address`. """
        # pylint: disable=too-many-arguments

        async_result = self.transfer_async(
            registry_address,
            token_address,
            amount,
            target,
            identifier,
        )
        return async_result.wait(timeout=timeout)

    def transfer_async(
            self,
            registry_address,
            token_address,
            amount,
            target,
            identifier=None):

        if not isinstance(amount, int):
            raise InvalidAmount('Amount not a number')

        if amount <= 0:
            raise InvalidAmount('Amount negative')

        if not is_binary_address(token_address):
            raise InvalidAddress('token address is not valid.')

        if not is_binary_address(target):
            raise InvalidAddress('target address is not valid.')

        valid_tokens = views.get_token_network_addresses_for(
            views.state_from_raiden(self.raiden),
            registry_address,
        )
        if token_address not in valid_tokens:
            raise UnknownTokenAddress('Token address is not known.')

        log.debug(
            'initiating transfer',
            initiator=pex(self.raiden.address),
            target=pex(target),
            token=pex(token_address),
            amount=amount,
            identifier=identifier,
        )

        payment_network_identifier = self.raiden.default_registry.address
        token_network_identifier = views.get_token_network_identifier_by_token_address(
            views.state_from_raiden(self.raiden),
            payment_network_identifier,
            token_address,
        )
        async_result = self.raiden.mediated_transfer_async(
            token_network_identifier,
            amount,
            target,
            identifier,
        )
        return async_result

    def get_network_events(self, registry_address, from_block, to_block):
        return get_all_registry_events(
            self.raiden.chain,
            registry_address,
            events=ALL_EVENTS,
            from_block=from_block,
            to_block=to_block,
        )

    def get_channel_events(self, channel_address, from_block, to_block='latest'):
        if not is_binary_address(channel_address):
            raise InvalidAddress(
                'Expected binary address format for channel in get_channel_events',
            )
        returned_events = get_all_netting_channel_events(
            self.raiden.chain,
            channel_address,
            events=ALL_EVENTS,
            from_block=from_block,
            to_block=to_block,
        )
        raiden_events = self.raiden.wal.storage.get_events_by_block(
            from_block=from_block,
            to_block=to_block,
        )
        # Here choose which raiden internal events we want to expose to the end user
        for block_number, event in raiden_events:
            if isinstance(event, EVENTS_EXTERNALLY_VISIBLE):
                new_event = {
                    'block_number': block_number,
                    'event': type(event).__name__,
                }
                new_event.update(event.__dict__)
                returned_events.append(new_event)

        return returned_events

    def get_token_network_events(self, token_address, from_block, to_block='latest'):

        if not is_binary_address(token_address):
            raise InvalidAddress(
                'Expected binary address format for token in get_token_network_events',
            )
        channel_manager_address = self.raiden.default_registry.manager_address_by_token(
            token_address,
        )
        if channel_manager_address is None:
            raise UnknownTokenAddress('Token address is not known.')

        returned_events = get_all_channel_manager_events(
            self.raiden.chain,
            channel_manager_address,
            events=ALL_EVENTS,
            from_block=from_block,
            to_block=to_block,
        )

        raiden_events = self.raiden.wal.storage.get_events_by_block(
            from_block=from_block,
            to_block=to_block,
        )

        # Here choose which raiden internal events we want to expose to the end user
        for block_number, event in raiden_events:
            if isinstance(event, EVENTS_EXTERNALLY_VISIBLE):
                new_event = {
                    'block_number': block_number,
                    'event': type(event).__name__,
                }
                new_event.update(event.__dict__)
                returned_events.append(new_event)

        return returned_events

    transfer = transfer_and_wait
