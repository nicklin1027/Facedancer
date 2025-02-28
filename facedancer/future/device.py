#
# This file is part of FaceDancer.
#
""" Functionality for defining USB devices. """

import sys
import asyncio
import logging
import warnings

from typing         import Coroutine, Dict, Iterable, Union
from dataclasses    import dataclass, field

from prompt_toolkit import HTML, print_formatted_text

from ..             import FacedancerUSBApp, LOGLEVEL_TRACE
from .types         import DescriptorTypes, LanguageIDs, USBStandardRequests
from .types         import USBDirection, USBRequestType, USBRequestRecipient

from .magic         import instantiate_subordinates

from .descriptor    import USBDescribable, USBDescriptor, StringDescriptorManager
from .configuration import USBConfiguration
from .endpoint      import USBEndpoint
from .request       import USBControlRequest, USBRequestHandler
from .request       import standard_request_handler, to_device, get_request_handler_methods


@dataclass
class USBBaseDevice(USBDescribable, USBRequestHandler):
    """
    Base-most class for FaceDancer USB devices. This version is very similar to the USBDevice type,
    except that it does not define _any_ standard handlers. This allows you the freedom to declare
    whatever standard requests you'd like.

    Fields:
        device_class/device_subclass/protocol_revision_number --
                The USB descriptor fields that select the class, subclass, and protocol.
        vendor_id, product_id --
                The USB vendor and product ID for this device.
        manufacturer_string, product_string, serial_number_string --
                Python strings identifying the device to the USB host.
        supported_languages --
                A tuple containing all of the language IDs supported by the device.
        device_revision --
                Number indicating the hardware revision of this device. Typically BCD.
        usb_spec_revision --
                Number indicating the version of the USB specification we adhere to. Typically 0x0200.
    """

    DESCRIPTOR_TYPE_NUMBER    = 0x01
    DESCRIPTOR_LENGTH         = 0x12

    name                     : str = "generic device"

    device_class             : int  = 0
    device_subclass          : int  = 0

    protocol_revision_number : int  = 0
    max_packet_size_ep0      : int  = 64

    vendor_id                : int  = 0x610b
    product_id               : int  = 0x4653

    manufacturer_string      : str  = "FaceDancer"
    product_string           : str  = "Generic USB Device"
    serial_number_string     : str  = "S/N 3420E"

    # I feel bad for putting this as the default language ID / propagating anglocentrism,
    # but this appears to be the only language ID supported by some systems, so here it is.
    supported_languages      : tuple = (LanguageIDs.ENGLISH_US,)

    device_revision          : int  = 0
    usb_spec_version         : int  = 0x0002

    descriptors              : Dict[int, Union[bytes, callable]] = field(default_factory=dict)
    configurations           : Dict[int, USBConfiguration]       = field(default_factory=dict)
    backend                  : FacedancerUSBApp = None


    def __post_init__(self):
        """ Set up our device for execution. """

        self.strings = StringDescriptorManager()

        # If we don't have a collection of descriptors, gather any attached to the class.
        subordinate_descriptors = instantiate_subordinates(self, USBDescriptor)
        self.descriptors.update(subordinate_descriptors)

        # Add our basic descriptor handlers.
        self.descriptors.update({
            DescriptorTypes.DEVICE:        lambda _ : self.get_descriptor(),
            DescriptorTypes.CONFIGURATION: self.get_configuration_descriptor,
            DescriptorTypes.STRING:        self.get_string_descriptor
        })

        # Start off un-configured, and with an address of 0.
        self.address = 0
        self.configuration = None

        # Populate our control request handlers, and any subordinate classes we'll need to create.
        self._request_handler_methods = get_request_handler_methods(self)
        self.configurations = instantiate_subordinates(self, USBConfiguration)

        # Create a set of suggested requests. We'll use this to store the vitals
        # of any unhandled requests, so we can provide user suggestions later.
        self._suggested_requests = set()
        self._suggested_request_metadata = {}


    #
    # Control interface.
    #

    def add_configuration(self, configuration: USBConfiguration):
        """ Adds the provided configuration to this device. """
        self.configurations[configuration.number] = configuration
        configuration.parent = self


    def connect(self):
        """ Connects this device to the host; e.g. turning on our presence-detect pull up. """
        if self.backend is None:
            self.backend = FacedancerUSBApp()

        self.backend.connect(self, self.max_packet_size_ep0)


    def disconnect(self):
        """ Disconnects this device from the host. """
        self.backend.disconnect()


    async def run(self):
        """ Runs the actual device emulation. """

        # Sanity check to avoid common issues.
        if len(self.configurations) == 0:
            logging.error("No configurations defined on the emulated device! "
                    "Did you forget @use_inner_classes_automatically?")

        if self.backend is None:
            self.connect()

        # Constantly service any events that need to be performed.
        while True:
            self.backend.service_irqs()
            await asyncio.sleep(0)


    def run_with(self, *coroutines: Iterable[Coroutine]):
        """
        Runs the actual device emulation synchronously; running any provided
        coroutines simultaneously.
        """

        async def inner():
            await asyncio.gather(self.run(), *coroutines)

        asyncio.run(inner())


    def emulate(self, *coroutines: Iterable[Coroutine]):
        """ Convenience method that runs a full method in a blocking manner.
        Performs connect, run, and then disconnect.

        Parameters:
            *coroutines -- any asyncio coroutines to be executed concurrently
                           with our emulation
        """

        self.connect()

        try:
            self.run_with(*coroutines)
        except KeyboardInterrupt:
            pass
        finally:
            self.disconnect()


    #
    # I/O interface.
    #

    def stall(self, *, endpoint_number: int = 0, direction: USBDirection = USBDirection.OUT):
        """ Stalls the provided endpoint.

        For endpoint zero, this indicates that the active (or next)
        request is not supported. For all other endpoints, this indicates
        a persistent 'halt' condition.

        Parameters:
            endpoint -- The endpoint address; or EP0 if not provided.
        """
        self.backend.stall_endpoint(endpoint_number, direction)


    # TODO: add a clear_stall() method here for non-control endpoints

    def send(self, endpoint_number: int, data: bytes, *, blocking: bool = False):
        """ Queues sending data on the IN endpoint with the provided number.

        Parameters:
            endpoint_number -- The endpoint number to send data upon.
            data            -- The data to send.
            blocking        -- If provided and true, this function will block
                               until the backend indicates the send is complete.
        """

        # EP0 is special, as it conceptually belongs to the whole device, as it's used
        # for control requests and configuration. We'll handle it directly, here.
        #
        # All of our backends currently automatically handle packetization and ZLPs for
        # the control endpoint, so we'll skip packetizing it (which would generate spurious ZLPs).
        if endpoint_number == 0:
            self.backend.send_on_endpoint(0, data, blocking=blocking)
        elif self.configuration:
            endpoint = self.configuration.get_endpoint(endpoint_number, USBDirection.IN)
            endpoint.send(data, blocking=blocking)


    def _send_in_packets(self, endpoint_number: int, data: bytes, *,
            packet_size: int, blocking: bool = False):
        """ Queues sending data on the IN endpoint with the provided number.

        Sends the relevant data to the backend in chunks of packet_size.

        Parameters:
            endpoint_number -- The endpoint number to send data upon.
            data            -- The data to send.
            packet_size     -- The "chunk" size to send in.
            blocking        -- If provided and true, this function will block
                               until the backend indicates the send is complete.
        """

        data = bytearray(data)

        # Special case: if we have a ZLP to begin with, send it, and return.
        if not data:
            self.backend.send_on_endpoint(endpoint_number, data, blocking=blocking)
            return

        # Send the relevant data one packet at a time,
        # chunking if we're larger than the max packet size.
        # This matches the behavior of the MAX3420E.
        while data:
            packet = data[0:packet_size]
            del data[0:packet_size]

            self.backend.send_on_endpoint(endpoint_number, packet, blocking=blocking)


    def get_endpoint(self, endpoint_number: int, direction: USBDirection) -> USBEndpoint:
        """ Attempts to find a subordinate endpoint matching the given number/direction.

        Parameters:
            endpoint_number -- The endpoint number to search for.
            direction       -- The endpoint direction to be matched.

        Returns:
            The matching endpoint; or None if no matching endpoint existed.
        """

        if self.configuration:
            return self.configuration.get_endpoint(endpoint_number, direction)
        else:
            return None


    #
    # Backend interface helpers.
    #
    def create_request(self, raw_data: bytes) -> USBControlRequest:
        return USBControlRequest.from_raw_bytes(raw_data, device=self)


    #
    # Backend / low-level event receivers.
    #


    def handle_nak(self, ep_num: int):
        """ Backend data-requested handler; for legacy compatibility.

        Prefer overriding handle_data_requested() and handle_unexpected_data_Requested
        """
        endpoint = self.get_endpoint(ep_num, USBDirection.IN)

        if endpoint:
            self.handle_data_requested(endpoint)
        else:
            self.handle_unexpected_data_requested(ep_num)


    def handle_buffer_available(self, ep_num):
        """ Backend data-buffer-empty handler; for legacy compatibility.

        Prefer overriding handle_buffer_available().
        """
        endpoint = self.get_endpoint(ep_num, USBDirection.IN)

        if endpoint:
            self.handle_buffer_empty(endpoint)


    def handle_data_available(self, ep_num, data):
        """ Backend data-available handler; for legacy compatibility.

        Prefer overriding handle_data_received().
        """
        endpoint = self.get_endpoint(ep_num, USBDirection.OUT)

        if endpoint:
            self.handle_data_received(endpoint, data)
        else:
            self.handle_unexpected_data_received(ep_num, data)


    #
    # Event handlers.
    #

    def handle_bus_reset(self):
        """ Event handler for a bus reset. """
        logging.info("Host issued a bus reset; resetting our connection.")

        # Clear our state back to address zero and no configuration.
        self.configuration = None
        self.address = 0

        self.backend.reset()


    def handle_request(self, request: USBControlRequest):
        """ Core control request handler.

        This function can be overridden by a subclass if desired; but the typical way to
        handle a specific control request is to the the ``@control_request_handler`` decorators.

        Parameters:
            request -- the USBControlRequest object representing the relevant request
        """
        logging.debug(f"{self.name} received request: {request}")

        # Call our base USBRequestHandler method.
        handled = super().handle_request(request)

        # As the top-most handle_request function, we have an extra responsibility:
        # we'll need to stall the endpoint if no handler was found.
        if not handled:
            logging.warning(f"Stalling unhandled {request}.")
            self._add_request_suggestion(request)
            self.stall()

        return handled


    def handle_data_received(self, endpoint: USBEndpoint, data: bytes):
        """ Handler for receipt of non-control request data.

        Typically, this method will delegate any data received to the
        appropriate configuration/interface/endpoint. If overridden, the
        overriding function will receive all data.

        Parameters:
            endpoint_number -- The endpoint number on which the data was received.
            data            -- The raw bytes received on the relevant endpoint.
        """

        # If we have a configuration, delegate to it.
        if self.configuration:
            self.configuration.handle_data_received(endpoint, data)

        # If we're un-configured, we don't expect to receive
        # anything other than control data; defer to our "unexpected data".
        else:
            logging.error(f"Received non-control data when unconfigured!"
                    "This is invalid host behavior.")
            self.handle_unexpected_data_received(endpoint.number, data)


    def handle_unexpected_data_received(self, endpoint_number: int, data: bytes):
        """ Handler for unexpected data.

        Handles any data directed at an unexpected target; e.g. an endpoint that
        doesn't exist. Note that even if `handle_data_received` is overridden,
        this method can still be called e.g. by configuration.handle_data_received.

        Parameters:
            endpoint_number -- The endpoint number on which the data was received.
            data            -- The raw bytes received on the relevant endpoint.
        """
        logging.error(f"Received {len(data)} bytes of data on invalid EP{endpoint_number}/OUT.")


    def handle_data_requested(self, endpoint: USBEndpoint):
        """ Handler called when the host requests data on a non-control endpoint.

        Typically, this method will delegate the request to the appropriate
        configuration+interface+endpoint. If overridden, the
        overriding function will receive all events.

        Parameters:
            endpoint_number -- The endpoint number on which the host requested data.
        """

        # If we have a configuration, delegate to it.
        if self.configuration:
            self.configuration.handle_data_requested(endpoint)

        # If we're un-configured, we don't expect to receive
        # anything other than control data; defer to our "unexpected data".
        else:
            logging.error(f"Received non-control data when unconfigured!"
                    "This is invalid host behavior.")
            self.handle_unexpected_data_requested(endpoint.number)


    def handle_unexpected_data_requested(self, endpoint_number: int):
        """ Handler for unexpected data requests.

        Handles any requests directed at an unexpected target; e.g. an endpoint that
        doesn't exist. Note that even if `handle_data_requested` is overridden,
        this method can still be called e.g. by configuration.handle_data_received.

        Parameters:
            endpoint_number -- The endpoint number the data was received.
        """
        logging.error(f"Host requested data on invalid EP{endpoint_number}/IN.")


    def handle_buffer_empty(self, endpoint: USBEndpoint):
        """ Handler called when a given endpoint first has an empty buffer.

        Often, an empty buffer indicates an opportunity to queue data
        for sending ('prime an endpoint'), but doesn't necessarily mean
        that the host is planning on reading the data.

        This function is called only once per buffer.
        """

        # If we have a configuration, delegate to it.
        if self.configuration:
            self.configuration.handle_buffer_empty(endpoint)


    #
    # Methods for USBRequestHandler.
    #

    def _request_handlers(self) -> Iterable[callable]:
        return self._request_handler_methods


    def _get_subordinate_handlers(self) -> Iterable[callable]:
        # As a device, our subordinates are our configurations.
        return self.configurations.values()


    #
    # Suggestion engine.
    #

    def _add_request_suggestion(self, request: USBControlRequest):
        """ Adds a 'suggestion' to the list of requests that may need implementing.

        Parameters:
            request -- The unhandled request on which the suggestion should be based.
         """

        # Build a tuple of the relevant immutable parts of the request,
        # and store it as a suggestion.
        suggestion_summary = (request.direction, request.type, request.recipient, request.number)

        self._suggested_requests.add(suggestion_summary)
        self._suggested_request_metadata[suggestion_summary] = {
            'length': request.length,
            'data':   request.data
        }


    def _print_suggested_requests(self):
        """ Prints a collection of suggested additions to the stdout. """

        # Create a quick printing shortcut.
        print_html = lambda data : print_formatted_text(HTML(data))

        # Look-ups for the function's decorators / etc.
        request_type_decorator = {
            USBRequestType.STANDARD:    '@standard_request_handler',
            USBRequestType.VENDOR:      '@vendor_request_handler',
            USBRequestType.CLASS:       '@class_request_handler',
            USBRequestRecipient.OTHER:  '@reserved_request_handler'
        }
        target_decorator = {
            USBRequestRecipient.DEVICE:    '@to_device',
            USBRequestRecipient.INTERFACE: '@to_this_interface',
            USBRequestRecipient.ENDPOINT:  '@to_this_endpoint',
            USBRequestRecipient.OTHER:     '@to_other',
        }

        print_html("\n<u>Request handler code:</u>")

        if not self._suggested_requests:
            print_html("\t No suggestions.")
            return

        # Print each suggestion.
        for suggestion in self._suggested_requests:
            direction, request_type, recipient, number = suggestion
            metadata = self._suggested_request_metadata[suggestion]

            # Find the associated text descriptions for the relevant field.
            decorator = request_type_decorator[request_type]
            direction_name = USBDirection(direction).name

            # Generate basic metadata for our function.
            request_number = f"<ansiblue>{number}</ansiblue>"
            function_name = f"handle_control_request_{number}"

            # Figure out if we want to use a cleaner request number.
            if request_type == USBRequestType.STANDARD:
                try:
                    request_number = f"USBStandardRequests.{USBStandardRequests(number).name}"
                    function_name  = f"handle_{USBStandardRequests(number).name.lower()}_request"
                except ValueError:
                    pass


            # Figure out if we should include a target decorator.
            if recipient in target_decorator:
                recipient_decorator = target_decorator[recipient]
                specific_recipient  = ""
            else:
                recipient_decorator = None
                specific_recipient = f"recipient=<ansiblue>{recipient}</ansiblue>, "

            #
            # Print the code block.
            #
            print_html("")

            # Primary request decorator, e.g. "@standard_request_handler".
            print_html(f"    <ansigreen>{decorator}</ansigreen>("
                    f"number={request_number}, "
                    f"{specific_recipient}"
                    f"direction=USBDirection.{direction_name}"
                    f")")

            # Recipient specifier; e.g. "@to_device"
            if recipient_decorator:
                print_html(f"    <ansigreen>{recipient_decorator}</ansigreen>")

            # Function definition.
            print_html(f"    <ansiwhite><b>def</b></ansiwhite> "
                    f"<ansiyellow>{function_name}</ansiyellow>"
                    "(self, request):"
            )

            # Note about the requested length, if applicable.
            if direction == USBDirection.IN:
                print_html(f"        <ansimagenta># Most recent request was for {metadata['length']}B of data.</ansimagenta>")
            else:
                print_html(f"        <ansimagenta># Most recent request data: {metadata['data']}.</ansimagenta>")

            # Default function body.
            print_html(f"        <ansimagenta># Replace me with your handler.</ansimagenta>")
            print_html(f"        request.stall()")


    def print_suggested_additions(self):
        """ Prints a collection of suggested additions to the stdout. """

        sys.stdout.flush()
        sys.stderr.flush()

        # Create a quick printing shortcut.
        print_html = lambda data : print_formatted_text(HTML(data))

        # Header.
        print_html("")
        print_html("<b><u>Automatic Suggestions</u></b>")
        print_html("These suggestions are based on simple observed behavior;")
        print_html("not all of these suggestions may be useful / desirable.")
        print_html("")

        self._print_suggested_requests()
        print_html("")


    #
    # Backend helpers.
    #

    def set_address(self, address: int, defer: bool = False):
        """ Updates the device's knowledge of its own address.

        Parameters:
            address -- The address to apply.
            defer   -- If true, the address change should be deferred
                       until the next time a control request ends. Should
                       be set if we're changing the address before we ack
                       the relevant transaction.
        """
        self.address = address
        self.backend.set_address(address, defer)


    def get_descriptor(self) -> bytes:
        """ Returns a complete descriptor for this device. """

        # FIXME: use construct, here!

        d = bytearray([
            18,         # length of descriptor in bytes
            1,          # descriptor type 1 == device
            (self.usb_spec_version >> 8) & 0xff,
            self.usb_spec_version & 0xff,
            self.device_class,
            self.device_subclass,
            self.protocol_revision_number,
            self.max_packet_size_ep0,
            self.vendor_id & 0xff,
            (self.vendor_id >> 8) & 0xff,
            self.product_id & 0xff,
            (self.product_id >> 8) & 0xff,
            (self.device_revision >> 8) & 0xff,
            self.device_revision & 0xff,
            self.strings.get_index(self.manufacturer_string),
            self.strings.get_index(self.product_string),
            self.strings.get_index(self.serial_number_string),
            len(self.configurations)
        ])
        return d


    def get_configuration_descriptor(self, index: int) -> bytes:
        """ Returns the configuration descriptor with the given configuration number. """

        # The index argument is zero-indexed; here, but configuration numbers
        # are one-indexed (as 0 is unconfigured). Adjust accordingly.
        return self.configurations[index + 1].get_descriptor()


    def handle_get_supported_languages_descriptor(self) -> bytes:
        """ Returns the special string-descriptor-zero that indicates which languages are supported. """

        # Our string descriptor is going to have two header bytes, plus two bytes
        # for each language.
        total_length = (len(self.supported_languages) * 2) + 2
        packet = bytearray([total_length, DescriptorTypes.STRING])

        for language in self.supported_languages:
            packet.extend(language.to_bytes(2, byteorder='little'))

        return bytes(packet)


    def get_string_descriptor(self, index:int) -> bytes:
        """ Returns the string descriptor associated with a given index. """

        if index == 0:
            return self.handle_get_supported_languages_descriptor()
        else:
            return self.strings[index]


    def handle_generic_get_descriptor_request(self, request: USBControlRequest):
        """ Handle GET_DESCRIPTOR requests; per USB2 [9.4.3] """

        logging.debug(f"received GET_DESCRIPTOR request {request}")

        # Extract the core parameters from the request.
        descriptor_type  = request.value_high
        descriptor_index = request.value_low

        # Try to find the descriptor associate with the request.
        response = self.descriptors.get(descriptor_type, None)

        # If we have a callable, we need to evaluate it to figure
        # out what the actual descriptor should be.
        while callable(response):
            response = response(descriptor_index)

        # If we wound up with a valid response, reply with it.
        if response:
            response_length = min(request.length, len(response))
            request.reply(response[:response_length])

            logging.log(LOGLEVEL_TRACE, f"sending {response_length} bytes in response")
        else:
            logging.log(LOGLEVEL_TRACE, f"stalling descriptor request")
            request.stall()



class USBDevice(USBBaseDevice):
    """ Class representing the behavior of a USB device.

    This default implementation provides standard request handlers
    in order to facilitate creating a host-compatible USB device.

    These functions can be overloaded to change their behavior. If
    you want to dramatically change the behavior of these requests,
    you can opt to use USBBaseDevice, which lacks standard request
    handling.

    Fields:
        device_class/device_subclass/protocol_revision_number --
                The USB descriptor fields that select the class, subclass, and protcol.
        vendor_id, product_id --
                The USB vendor and product ID for this device.
        manufacturer_string, product_string, serial_number_string --
                Python strings identifying the device to the USB host.
        supported_languages --
                A tuple containing all of the language IDs supported by the device.
        device_revision --
                Number indicating the hardware revision of this device. Typically BCD.
        usb_spec_revision --
                Number indicating the version of the USB specification we adhere to. Typically 0x0200.
    """


    @standard_request_handler(number=USBStandardRequests.GET_STATUS)
    @to_device
    def handle_get_status_request(self, request):
        """ Handles GET_STATUS requests; per USB2 [9.4.5]."""

        logging.debug("received GET_STATUS request")

        # self-powered and remote-wakeup (USB 2.0 Spec section 9.4.5)
        request.reply(b'\x03\x00')


    @standard_request_handler(number=USBStandardRequests.CLEAR_FEATURE)
    @to_device
    def handle_clear_feature_request(self, request):
        """ Handle CLEAR_FEATURE requests; per USB2 [9.4.1] """
        logging.debug(f"Received CLEAR_FEATURE request with type {request.number} and value {request.value}.")
        request.acknowledge()


    @standard_request_handler(number=USBStandardRequests.SET_FEATURE)
    @to_device
    def handle_set_feature_request(self, request):
        """ Handle SET_FEATURE requests; per USB2 [9.4.9] """
        logging.debug("received SET_FEATURE request")
        request.stall()


    @standard_request_handler(number=USBStandardRequests.SET_ADDRESS)
    @to_device
    def handle_set_address_request(self, request):
        """ Handle SET_ADDRESS requests; per USB2 [9.4.6] """
        request.acknowledge(blocking=True)
        self.set_address(request.value)


    @standard_request_handler(number=USBStandardRequests.GET_DESCRIPTOR)
    @to_device
    def handle_get_descriptor_request(self, request):
        """ Handle GET_DESCRIPTOR requests; per USB2 [9.4.3] """

        # Defer to our generic get_descriptor handler.
        self.handle_generic_get_descriptor_request(request)



    @standard_request_handler(number=USBStandardRequests.SET_DESCRIPTOR)
    @to_device
    def handle_set_descriptor_request(self, request):
        """ Handle SET_DESCRIPTOr requests; per USB2 [9.4.8] """
        logging.debug("received SET_DESCRIPTOR request")
        request.stall()


    @standard_request_handler(number=USBStandardRequests.GET_CONFIGURATION)
    @to_device
    def handle_get_configuration_request(self, request):
        """ Handle GET_CONFIGURATION requests; per USB2 [9.4.2] """
        logging.debug(f"received GET_CONFIGURATION request for configuration {request.value}")

        # If we haven't yet been configured, send back a zero configuration value.
        if self.configuration is None:
            request.reply(b"\x00")

        # Otherwise, return the index for our configuration.
        else:
            config_index = self.configuration.number
            request.reply(config_index.to_bytes(1, byteorder='little'))


    @standard_request_handler(number=USBStandardRequests.SET_CONFIGURATION)
    @to_device
    def handle_set_configuration_request(self, request):
        """ Handle SET_CONFIGURATION requests; per USB2 [9.4.7] """
        logging.debug("received SET_CONFIGURATION request")

        # If the host is requesting configuration zero, they're asking
        # us to drop our configuration.
        if request.value == 0:
            self.configuration = None
            request.acknowledge()

        # Otherwise, we'll find a given configuration and apply it.
        else:
            try:
                self.configuration = self.configurations[request.value]
                request.acknowledge()
            except KeyError:
                request.stall()

        # Notify the backend of the reconfiguration, in case
        # it needs to e.g. set up endpoints accordingly
        self.backend.configured(self.configuration)


    @standard_request_handler(number=USBStandardRequests.GET_INTERFACE)
    @to_device
    def handle_get_interface_request(self, request):
        """ Handle GET_INTERFACE requests; per USB2 [9.4.4] """
        logging.debug("received GET_INTERFACE request")

        # TODO: support alternate interfaces.
        # Since we don't support alternate interfaces [yet], we'll always
        # indicate use of interface zero.
        if self.configuration and (request.index_low in self.configuration.interfaces):
            request.reply(b'\x00')
        else:
            request.stall()


    @standard_request_handler(number=USBStandardRequests.SET_INTERFACE)
    @to_device
    def handle_set_interface_request(self, request):
        """ Handle SET_INTERFACE requests; per USB2 [9.4.10] """
        logging.debug(f"f{self.name} received SET_INTERFACE request")

        # We don't support alternate interfaces; so ACK setting
        # interface zero, and stall all others.
        if request.index_low == 0:
            request.acknowledge()
        else:
            request.stall()


    # USB 2.0 specification, section 9.4.11 (p 288 of pdf)
    @standard_request_handler(number=USBStandardRequests.SYNCH_FRAME)
    @to_device
    def handle_synch_frame_request(self, request):
        """ Handle SYNC_FRAME requests; per USB2 [9.4.10] """
        logging.debug(f"f{self.name} received SYNCH_FRAME request")
        request.acknowledge()
