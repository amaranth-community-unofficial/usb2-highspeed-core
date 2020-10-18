#
# This file is part of LUNA.
#
# Copyright (c) 2020 Great Scott Gadgets <info@greatscottgadgets.com>
# SPDX-License-Identifier: BSD-3-Clause
""" Control-request interfacing and gateware for USB3. """

from nmigen import *

from ...request             import SetupPacket
from ...stream              import SuperSpeedStreamInterface
from ..protocol.transaction import HandshakeGeneratorInterface, HandshakeReceiverInterface
from ..protocol.data        import DataHeaderPacket

from ....utils              import falling_edge_detected

class SuperSpeedRequestHandlerInterface:
    """ Interface representing a connection between a control endpoint and a request handler.

    Attributes
    ----------
    setup: SetupPacket()
        The setup packet relevant to any
    """

    MAX_PACKET_LENGTH = 1024

    def __init__(self):
        # Event signaling.
        self.setup                 = SetupPacket()
        self.data_requested        = Signal()
        self.status_requested      = Signal()

        # Receiver interface.
        self.rx                    = SuperSpeedStreamInterface()

        # Transmitter interface.
        self.tx                    = SuperSpeedStreamInterface()
        self.tx_length             = Signal(range(self.MAX_PACKET_LENGTH + 1))
        self.tx_sequence_number    = Signal(5)

        # Handshake interface.
        self.handshakes_out        = HandshakeGeneratorInterface()
        self.handshakes_in         = HandshakeReceiverInterface()

        # Device state management.
        self.address_changed       = Signal()
        self.new_address           = Signal(7)

        self.active_config         = Signal(8)
        self.config_changed        = Signal()
        self.new_config            = Signal(8)




class SuperSpeedSetupDecoder(Elaboratable):
    """ Gateware that decodes any received Setup packets.

    Attributes
    -----------
    sink: SuperSpeedStreamInterface(), input stream [read-only]
        Packet interface that carres in new data packets. Results should be considered questionable
        until :attr:``packet_good`` or :attr:``packet_bad`` are strobed.

    rx_good: Signal(), input
        Strobe; indicates that the packet received passed validations and can be considered good.
    rx_bad: Signal(), input
        Strobe; indicates that the packet failed CRC checks, or did not end properly.

    header_in: DataHeaderPacket(), input
        Header associated with the active packet.

    packet: SetupPacket(), output
        The parsed contents of our setup packet.
    """

    def __init__(self):

        #
        # I/O port
        #
        self.sink       = SuperSpeedStreamInterface()

        self.rx_good    = Signal()
        self.rx_bad     = Signal()

        self.header_in  = DataHeaderPacket()

        self.packet     = SetupPacket()


    def elaborate(self, platform):
        m = Module()

        # Capture our packet locally, until we have an entire valid packet.
        packet = SetupPacket()

        # Keep our "received" flag low unless explicitly driven.
        m.d.ss += self.packet.received.eq(0)

        with m.FSM(domain="ss"):

            # WAIT_FOR_FIRST -- we're waiting for the first word of a setup packet;
            # which we'll handle on receipt.
            with m.State("WAIT_FOR_FIRST"):
                packet_starting = self.sink.valid.all() & self.sink.first
                packet_is_setup = (self.header_in.setup)

                # Once we see the start of a new setup packet, parse it, and move to the second word.
                with m.If(packet_starting & packet_is_setup):
                    m.d.ss += packet.word_select(0, 32).eq(self.sink.data)
                    m.next = "PARSE_SECOND"

            # PARSE_SECOND -- handle the second and last packet, which contains the remainder of
            # our setup data.
            with m.State("PARSE_SECOND"):

                with m.If(self.sink.valid.all()):

                    # This should be our last word; parse it.
                    with m.If(self.sink.last):
                        m.d.ss += packet.word_select(1, 32).eq(self.sink.data)
                        m.next = "WAIT_FOR_VALID"

                    # If this wasn't our last word, something's gone very wrong.
                    # We'll ignore this packet.
                    with m.Else():
                        m.next = "WAIT_FOR_FIRST"

                # If we see :attr:``rx_bad``, this means our packet aborted early,
                # and thus isn't a valid setup packet. Ignore it, and go back to waiting
                # for our first packet.
                with m.If(self.rx_bad):
                        m.next = "WAIT_FOR_FIRST"

            # WAIT_FOR_VALID -- we've now received all of our data; and we're just waiting
            # for an indication of  whether the data is good or bad.
            with m.State("WAIT_FOR_VALID"):

                # If we see :attr:``packet_good``, this means we have a valid setup packet!
                # We'll output it, and indicate that we've received a new packet.
                with m.If(self.rx_good):
                    m.d.ss += [
                        # Output our stored packet...
                        self.packet           .eq(packet),

                        # ... but strobe its received flag for a cycle.
                        self.packet.received  .eq(1)
                    ]
                    m.next = "WAIT_FOR_FIRST"

                # If we see :attr:``packet_bad``, this means our packet failed CRC checks.
                # We can't do anything with it; so we'll just ignore it.
                with m.If(self.rx_bad):
                    m.next = "WAIT_FOR_FIRST"

        return m



class SuperSpeedRequestHandlerMultiplexer(Elaboratable):
    """ Multiplexes multiple RequestHandlers down to a single interface.

    Interfaces are added using .add_interface().

    Attributes
    ----------
    shared: SuperSpeedRequestHandlerInterface()
        The post-multiplexer RequestHandler interface.
    """

    def __init__(self):

        #
        # I/O port
        #
        self.shared = SuperSpeedRequestHandlerInterface()

        #
        # Internals
        #
        self._interfaces = []


    def add_interface(self, interface: SuperSpeedRequestHandlerInterface):
        """ Adds a RequestHandlerInterface to the multiplexer.

        Arbitration is not performed; it's expected only one handler will be
        driving requests at a time.
        """
        self._interfaces.append(interface)


    def _multiplex_signals(self, m, *, when, multiplex, sub_bus=None):
        """ Helper that creates a simple priority-encoder multiplexer.

        Parmeters:
            when      -- The name of the interface signal that indicates that the `multiplex` signals
                         should be selected for output. If this signals should be multiplex, it
                         should be included in `multiplex`.
            multiplex -- The names of the interface signals to be multiplexed.
        """

        def get_signal(interface, name):
            """ Fetches an interface signal by name / sub_bus. """

            if sub_bus:
                bus = getattr(interface, sub_bus)
                return getattr(bus, name)
            else:
                return  getattr(interface, name)


        # We're building an if-elif tree; so we should start with an If entry.
        conditional = m.If

        for interface in self._interfaces:
            condition = get_signal(interface, when)

            with conditional(condition):

                # Connect up each of our signals.
                for signal_name in multiplex:

                    # Get the actual signals for our input and output...
                    driving_signal = get_signal(interface,   signal_name)
                    target_signal  = get_signal(self.shared, signal_name)

                    # ... and connect them.
                    m.d.comb += target_signal   .eq(driving_signal)

            # After the first element, all other entries should be created with Elif.
            conditional = m.Elif



    def elaborate(self, platform):
        m = Module()
        shared = self.shared


        #
        # Pass through signals being routed -to- our pre-mux interfaces.
        #
        for interface in self._interfaces:
            m.d.comb += [

                # State inputs.
                shared.setup                     .connect(interface.setup),
                interface.active_config          .eq(shared.active_config),

                # Event inputs.
                interface.data_requested         .eq(shared.data_requested),
                interface.status_requested       .eq(shared.status_requested),

                # Receiver inputs.
                shared.rx                        .connect(interface.rx),
                shared.handshakes_in             .connect(interface.handshakes_in),
            ]

        #
        # Multiplex the signals being routed -from- our pre-mux interface.
        #
        self._multiplex_signals(m,
            when='address_changed',
            multiplex=['address_changed', 'new_address']
        )

        self._multiplex_signals(m,
            when='config_changed',
            multiplex=['config_changed', 'new_config']
        )

        #
        # Multiplex each of our transmit interfaces.
        #
        for interface in self._interfaces:

            # If the transmit interface is valid, connect it up to our endpoint.
            # The latest assignment will win; so we can treat these all as a parallel 'if's
            # and still get an appropriate priority encoder.
            with m.If(interface.tx.valid.any()):
                m.d.comb += [
                    shared.tx                  .stream_eq(interface.tx),
                    shared.tx_sequence_number  .eq(interface.tx_sequence_number),
                    shared.tx_length           .eq(interface.tx_length)
                ]


        #
        # Multiplex each of our handshake-out interfaces.
        #
        for interface in self._interfaces:
            any_generate_signal_asserted = (
                interface.handshakes_out.send_ack   |
                interface.handshakes_out.send_stall
            )

            # If the given interface is trying to send an handshake, connect it up
            # to our shared interface.
            with m.If(any_generate_signal_asserted):
                m.d.comb += shared.handshakes_out.connect(interface.handshakes_out)


        return m


class StallOnlyRequestHandler(Elaboratable):
    """ Simple gateware request handler that only conditionally stalls requests.

    I/O port:
        *: interface -- The RequestHandlerInterface used to handle requests.
                        See its record definition for signal definitions.
    """

    def __init__(self, stall_condition):
        """
        Parameters:
            stall_condition -- A function that accepts a SetupRequest packet, and returns
                               an nMigen conditional indicating whether we should stall.
        """

        self.condition = stall_condition

        #
        # I/O port
        #
        self.interface = SuperSpeedRequestHandlerInterface()


    def elaborate(self, platform):
        m = Module()
        interface = self.interface

        # If we have the opportunity to stall ...
        data_received = falling_edge_detected(m, interface.rx.valid, domain="ss")
        with m.If(interface.data_requested | interface.status_requested | data_received):

            # ... and our stall condition is met ...
            with m.If(self.condition(self.interface.setup)):

                # ... do so.
                m.d.comb += self.interface.handshakes_out.stall.eq(1)

        return m

