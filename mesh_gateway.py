""" mesh_gateway.py - A example of using the goTenna API for a command line messaging application
                      and SMS gateway.

Usage: python mesh_gateway.py
"""
from __future__ import print_function
import cmd # for the command line application
import sys # To quit
import os
import traceback
import logging
import math
import goTenna # The goTenna API
import re
import serial
import time
from time import sleep
import cbor
import threading
import socket
import select
import requests
import json
import configparser
from threading import Thread
from datetime import datetime, timedelta

BYTE_STRING_CBOR_TAG = 24
PHONE_NUMBER_CBOR_TAG = 25
MESSAGE_TEXT_CBOR_TAG = 26
SEGMENT_NUMBER_CBOR_TAG = 28
SEGMENT_COUNT_CBOR_TAG = 29

# For SPI connection only, set SPI_CONNECTION to true with proper SPI settings
SPI_CONNECTION = False
SPI_BUS_NO = 0
SPI_CHIP_NO = 0
SPI_REQUEST = 22
SPI_READY = 27

# For socket connections
DEFAULT_BUF_SIZE = 6000
MESH_PAYLOAD_SIZE = 150

# Configure the Python logging module to print to stderr. In your application,
# you may want to route the logging elsewhere.
logging.basicConfig()

# Import readline if the system has it
try:
    import readline
    assert readline # silence pyflakes
except ImportError:
    pass

def build_callback(in_flight_events, error_handler=None):
    """ Build a callback for sending to the API thread. May speciy a callable
    error_handler(details) taking the error details from the callback. The handler should return a string.
    """
    def default_error_handler(details):
        """ Easy error handler if no special behavior is needed. Just builds a string with the error.
        """
        if details['code'] in [goTenna.constants.ErrorCodes.TIMEOUT,
                                goTenna.constants.ErrorCodes.OSERROR,
                                goTenna.constants.ErrorCodes.EXCEPTION]:
            return "USB connection disrupted"
        return "Error: {}: {}".format(details['code'], details['msg'])

    # Define a second function here so it implicitly captures self
    captured_error_handler = [error_handler]
    def callback(correlation_id, success=None, results=None,
                    error=None, details=None):
        """ The default callback to pass to the API.

        See the documentation for ``goTenna.driver``.

        Does nothing but print whether the method succeeded or failed.
        """
        method = in_flight_events.pop(correlation_id.bytes, 'Method call')
        if success:
            if results:
                print("{} succeeded: {}".format(method, results))
            else:
                print("{} succeeded!".format(method))
        elif error:
            if not captured_error_handler[0]:
                captured_error_handler[0] = default_error_handler
            print("{} failed: {}".format(method, captured_error_handler[0](details)))
    return callback

class goTennaCLI(cmd.Cmd):
    """ CLI handler function
    """
    def __init__(self):
        self.api_thread = None
        self.status = {}
        cmd.Cmd.__init__(self)
        self.prompt = 'Mesh Gateway>'
        self.in_flight_events = {}
        self._set_frequencies = False
        self._set_tx_power = False
        self._set_bandwidth = False
        self._set_geo_region = False
        self._settings = goTenna.settings.GoTennaSettings(
            rf_settings=goTenna.settings.RFSettings(), 
            geo_settings=goTenna.settings.GeoSettings())
        self._do_encryption = False
        self._awaiting_disconnect_after_fw_update = [False]
        self.serial_port = None
        self.serial_rate = 115200
        self.sms_sender_dict = {}
        self.serial = None

        # imeshyou information
        self.email = ''
        self.password = ''
        self.node_name = None
        self.latlong = []
        self.range = 1
        self.use_tags = []
        self.node_id = None
        self.imeshyou_thread = None
        self.imeshyou_last_update = None
        
        self.session_token = None
        self.user_id = None

        # prevent threads from accessing serial port simultaneiously
        self.serial_lock = threading.Lock() 

    def precmd(self, line):
        if not self.api_thread\
           and not line.startswith('sdk_token')\
           and not line.startswith('quit'):
            print("An SDK token must be entered to begin.")
            return ''
        return line

    def do_sdk_token(self, rst):
        """ Enter an SDK token to begin usage of the driver. Usage: sdk_token TOKEN"""
        if self.api_thread:
            print("To change SDK tokens, restart the sample app.")
            return
        try:
            if not SPI_CONNECTION:
                self.api_thread = goTenna.driver.Driver(sdk_token=rst, gid=None, 
                                                    settings=None, 
                                                    event_callback=self.event_callback)
            else:
                self.api_thread = goTenna.driver.SpiDriver(
                                    SPI_BUS_NO, SPI_CHIP_NO, 22, 27,
                                    rst, None, None, self.event_callback)
            self.api_thread.start()
        except ValueError:
            print("SDK token {} is not valid. Please enter a valid SDK token."
                  .format(rst))

    def emptyline(self):
        pass

    def event_callback(self, evt):
        """ The event callback that will print messages from the API.

        See the documentation for ``goTenna.driver``.

        This will be invoked from the API's thread when events are received.
        """
        if evt.event_type == goTenna.driver.Event.MESSAGE:
            try:
                if type(evt.message.payload) == goTenna.payload.BinaryPayload:
                    protocol_msg = cbor.loads(evt.message.payload._binary_data)
                    if PHONE_NUMBER_CBOR_TAG in protocol_msg:
                        phone_number = str(protocol_msg[PHONE_NUMBER_CBOR_TAG])
                        text_message = protocol_msg[MESSAGE_TEXT_CBOR_TAG]
                        self.do_send_sms("+" + phone_number + " " + text_message)
                        self.sms_sender_dict[phone_number.encode()] = str(evt.message.sender.gid_val).encode()
                elif type(evt.message.payload) == goTenna.payload.CustomPayload:
                    print("Unknown BinaryPayload.")
                else:
                    print("Gateway received text message: " + evt.message.payload.message)
            except Exception: # pylint: disable=broad-except
                traceback.print_exc()
        elif evt.event_type == goTenna.driver.Event.DEVICE_PRESENT:
            print(str(evt))
            if self._awaiting_disconnect_after_fw_update[0]:
                print("Device physically connected")
            else:
                print("Device physically connected, configure to continue")
        elif evt.event_type == goTenna.driver.Event.CONNECT:
            if self._awaiting_disconnect_after_fw_update[0]:
                print("Device reconnected! Firmware update complete!")
                self._awaiting_disconnect_after_fw_update[0] = False
            else:
                print("Connected!")
                print(str(evt))
        elif evt.event_type == goTenna.driver.Event.DISCONNECT:
            if self._awaiting_disconnect_after_fw_update[0]:
                # Do not reset configuration so that the device will reconnect on its own
                print("Firmware update: Device disconnected, awaiting reconnect")
            else:
                print("Disconnected! {}".format(evt))
                # We reset the configuration here so that if the user plugs in a different
                # device it is not immediately reconfigured with new and incorrect data
                self.api_thread.set_gid(None)
                self.api_thread.set_rf_settings(None)
                self._set_frequencies = False
                self._set_tx_power = False
                self._set_bandwidth = False
        elif evt.event_type == goTenna.driver.Event.STATUS:
            self.status = evt.status
            if self.serial != None:
                # check for unread SMS messages
                self.do_read_sms("", self.forward_to_mesh)

        elif evt.event_type == goTenna.driver.Event.GROUP_CREATE:
            index = -1
            for idx, member in enumerate(evt.group.members):
                if member.gid_val == self.api_thread.gid.gid_val:
                    index = idx
                    break
            print("Added to group {}: You are member {}"
                  .format(evt.group.gid.gid_val,
                          index))

    def do_set_gid(self, rem):
        """ Create a new profile (if it does not already exist) with default settings.

        Usage: make_profile GID

        GID should be a 15-digit numerical GID.
        """
        if self.api_thread.connected:
            print("Must not be connected when setting GID")
            return
        (gid, _) = self._parse_gid(rem, goTenna.settings.GID.PRIVATE)
        if not gid:
            return
        self.api_thread.set_gid(gid)

    def do_create_group(self, rem):
        """ Create a new group and send invitations to other members.

        Usage create_group GIDs...

        GIDs should be a list of the private GIDs of the other members of the group. The group will be created and stored on the connected goTenna and invitations will be sent to the other members.
        """
        if not self.api_thread.connected:
            print("Must be connected when creating a group")
            return
        gids = [self.api_thread.gid]
        while True:
            (this_gid, rem) = self._parse_gid(rem,
                                              goTenna.settings.GID.PRIVATE,
                                              False)
            if not this_gid:
                break
            gids.append(this_gid)
        if len(gids) < 2:
            print("The group must have at least one other member.")
            return
        group = goTenna.settings.Group.create_new(gids)
        def _invite_callback(correlation_id, member_index,
                             success=None, error=None, details=None):
            if success:
                msg = 'succeeded'
                to_print = ''
            elif error:
                msg = 'failed'
                to_print = ': ' + str(details)
            print("Invitation of {} to {} {}{}".format(gids[member_index],
                                                       group.gid.gid_val,
                                                       msg, to_print))
        def method_callback(correlation_id, success=None, results=None,
                            error=None, details=None):
            """ Custom callback for group creation
            """
            # pylint: disable=unused-argument
            if success:
                print("Group {} created!".format(group.gid.gid_val))
            elif error:
                print("Group {} could not be created: {}: {}"
                      .format(group.gid.gid_val,
                              details['code'], details['msg']))
        print("Creating group {}".format(group.gid.gid_val))
        corr_id = self.api_thread.add_group(group,
                                            method_callback,
                                            True,
                                            _invite_callback)
        self.in_flight_events[corr_id.bytes] = 'Group creation of {}'\
            .format(group.gid.gid_val)

    def do_resend_invite(self, rem):
        """ Resend an invitation to a group to a specific member.

        Usage resend_invite GROUP_GID MEMBER_GID

        The GROUP_GID must be a previously-created group.
        The MEMBER_GID must be a previously-specified member of the group.
        """
        if not self.api_thread.connected:
            print("Must be connected when resending a group invite")
            return
        group_gid, rem = self._parse_gid(rem, goTenna.settings.GID.GROUP)
        member_gid, rem = self._parse_gid(rem, goTenna.settings.GID.PRIVATE)
        if not group_gid or not member_gid:
            print("Must specify group GID and member GID to invite")
            return
        group_to_invite = None
        for group in self.api_thread.groups:
            if group.gid.gid_val == group_gid.gid_val:
                group_to_invite = group
                break
        else:
            print("No group found matching GID {}".format(group_gid.gid_val))
            return
        member_idx = None
        for idx, member in enumerate(group_to_invite.members):
            if member.gid_val == member_gid.gid_val:
                member_idx = idx
                break
        else:
            print("Group {} has no member {}".format(group_gid.gid_val,
                                                     member_gid.gid_val))
            return
        def ack_callback(correlation_id, success):
            if success:
                print("Invitation of {} to {}: delivery confirmed"
                      .format(member_gid.gid_val, group_gid.gid_val))
            else:
                print("Invitation of {} to {}: delivery unconfirmed, recipient may be offline or out of range"
                      .format(member_gid.gid_val, group_gid.gid_val))
        corr_id = self.api_thread.invite_to_group(group_to_invite, member_idx,
                                                  build_callback(self.in_flight_events),
                                                  ack_callback=ack_callback)
        self.in_flight_events[corr_id.bytes] = 'Invitation of {} to {}'\
            .format(group_gid.gid_val, member_gid.gid_val)

    def do_remove_group(self, rem):
        """ Remove a group.

        Usage remove_group GROUP_GID

        GROUP_GID should be a group GID.
        """
        if not self.api_thread.connected:
            print("Must be connected when resending a group invite")
            return
        group_gid, rem = self._parse_gid(rem, goTenna.settings.GID.GROUP)

        if not group_gid:
            print("Must specify group GID to remove it")
            return

        group_to_remove = None
        for group in self.api_thread.groups:
            if group.gid.gid_val == group_gid.gid_val:
                group_to_remove = group
                break
        else:
            print("No group found matching GID {}".format(group_gid.gid_val))
            return

        def method_callback(correlation_id, success=None, results=None,
                            error=None, details=None):
            # logger.debug(" ")
            """ Custom callback for group removal
            """
            # pylint: disable=unused-argument
            if success:
                print("Group {} removed!".format(group_to_remove.gid.gid_val))
            elif error:
                print("Group {} could not be removed: {}: {}"
                      .format(group_gid.gid_val,
                              details['code'], details['msg']))

        corr_id = self.api_thread.remove_group(group, method_callback)
        self.in_flight_events[corr_id.bytes] = 'Group removing of {}' \
            .format(group_gid.gid_val)

    def preloop(self):
        """Initialization before prompting user for commands.
           Despite the claims in the Cmd documentaion, Cmd.preloop() is not a stub.
        """
        cmd.Cmd.preloop(self)   ## sets up command completion
        self._hist    = []      ## No history yet
        self._locals  = {}      ## Initialize execution namespace for user
        self._globals = {}
        
        if self.serial_port != None:
            self.do_init_sms("")

        if self.serial != None:
            self.do_delete_sms("")

    def do_quit(self, arg):
        """ Safely quit.

        Usage: quit
        """
        # pylint: disable=unused-argument
        if self.api_thread:
            self.api_thread.join()
        if self.serial and self.serial.is_open:
            self.serial.close()
            self.serial = None
        if self.imeshyou_thread:
            self.imeshyou_last_update = None
            self.imeshyou_thread.join()

        return True

    def do_echo(self, rem):
        """ Send an echo command

        Usage: echo
        """
        if not self.api_thread.connected:
            print("No device connected")
        else:
            def error_handler(details):
                """ A special error handler for formatting message failures
                """
                if details['code'] in [goTenna.constants.ErrorCodes.TIMEOUT,
                                       goTenna.constants.ErrorCodes.OSERROR]:
                    return "Echo command may not have been sent: USB connection disrupted"
                return "Error sending echo command: {}".format(details)

            try:
                method_callback = build_callback(self.in_flight_events, error_handler)
                corr_id = self.api_thread.echo(method_callback)
            except ValueError:
                print("Echo failed!")
                return
            self.in_flight_events[corr_id.bytes] = 'Echo Send'

    def do_send_broadcast(self, message):
        """ Send a broadcast message

        Usage: send_broadcast MESSAGE
        """
        if not self.api_thread.connected:
            print("No device connected")
        else:
            try:
                method_callback = build_callback(self.in_flight_events)
                payload = goTenna.payload.TextPayload(message)
                corr_id = self.api_thread.send_broadcast(payload,
                                                         method_callback)
            except ValueError:
                print("Message too long!")
                return
            self.in_flight_events[corr_id.bytes] = 'Broadcast message: {}'.format(message)

    @staticmethod
    def _parse_gid(line, gid_type, print_message=True):
        parts = line.split(' ')
        remainder = ' '.join(parts[1:])
        gidpart = parts[0]
        try:
            gid = int(gidpart)
            if gid > goTenna.constants.GID_MAX:
                print('{} is not a valid GID. The maximum GID is {}'
                      .format(str(gid), str(goTenna.constants.GID_MAX)))
                return (None, line)
            gidobj = goTenna.settings.GID(gid, gid_type)
            return (gidobj, remainder)
        except ValueError:
            if print_message:
                print('{} is not a valid GID.'.format(line))
            return (None, remainder)

    def do_send_private(self, rem):
        """ Send a private message to a contact

        Usage: send_private GID MESSAGE

        GID is the GID to send the private message to.

        MESSAGE is the message.
        """
        if not self.api_thread.connected:
            print("Must connect first")
            return
        (gid, rest) = self._parse_gid(rem, goTenna.settings.GID.PRIVATE)
        if not gid:
            return
        message = rest

        try:
            method_callback = build_callback(self.in_flight_events)
            payload = goTenna.payload.TextPayload(message)
            def ack_callback(correlation_id, success):
                if success:
                    print("Private message to {}: delivery confirmed"
                          .format(gid.gid_val))
                else:
                    print("Private message to {}: delivery not confirmed, recipient may be offline or out of range"
                          .format(gid.gid_val))
            corr_id = self.api_thread.send_private(gid, payload,
                                                   method_callback,
                                                   ack_callback=ack_callback,
                                                   encrypt=self._do_encryption)
        except ValueError:
            print("Message too long!")
            return
        self.in_flight_events[corr_id.bytes]\
            = 'Private message to {}: {}'.format(gid.gid_val, message)

    def do_send_group(self, rem):
        """ Send a message to a group.

        Usage: send_group GROUP_GID MESSAGE

        GROUP_GID is the GID of the group to send the message to. This must have been previously loaded into the API, whether by receiving an invitation, using add_group, or using create_group.
        """
        if not self.api_thread.connected:
            print("Must connect first.")
            return
        (gid, rest) = self._parse_gid(rem, goTenna.settings.GID.GROUP)
        if not gid:
            return
        message = rest
        group = None
        for group in self.api_thread.groups:
            if gid.gid_val == group.gid.gid_val:
                group = group
                break
        else:
            print("Group {} is not known".format(gid.gid_val))
            return
        try:
            payload = goTenna.payload.TextPayload(message)
            corr_id = self.api_thread.send_group(group, payload,
                                                 build_callback(self.in_flight_events),
                                                 encrypt=self._do_encryption)
        except ValueError:
            print("message too long!")
            return
        self.in_flight_events[corr_id.bytes] = 'Group message to {}: {}'\
            .format(group.gid.gid_val, message)

    def get_device_type(self):
        return self.api_thread.device_type

    def do_set_transmit_power(self, rem):
        """ Set the transmit power of the device.

        Usage: set_transmit_power POWER

        POWER should be a string, one of 'HALF_W', 'ONE_W', 'TWO_W' or 'FIVE_W'
        """
        if self.get_device_type() == "900":
             print("This configuration cannot be done for Mesh devices.")
             return
        ok_args = [attr
                   for attr in dir(goTenna.constants.POWERLEVELS)
                   if attr.endswith('W')]
        if rem.strip() not in ok_args:
            print("Invalid power setting {}".format(rem))
            return
        power = getattr(goTenna.constants.POWERLEVELS, rem.strip())
        self._set_tx_power = True
        self._settings.rf_settings.power_enum = power
        self._maybe_update_rf_settings()

    def do_list_bandwidth(self, rem):
        """ List the available bandwidth.

        Usage: list_bandwidth
        """
        print("Allowed bandwidth in kHz: {}"
                .format(str(goTenna.constants.BANDWIDTH_KHZ[0].allowed_bandwidth)))

    def do_set_bandwidth(self, rem):
        """ Set the bandwidth for the device.

        Usage: set_bandwidth BANDWIDTH

        BANDWIDTH should be a bandwidth in kHz.

        Allowed bandwidth can be displayed with list_bandwidth.
        """
        if self.get_device_type() == "900":
            print("This configuration cannot be done for Mesh devices.")
            return
        bw_val = float(rem.strip())
        for bw in goTenna.constants.BANDWIDTH_KHZ:
            if bw.bandwidth == bw_val:
                bandwidth = bw
                break
        else:
            print("{} is not a valid bandwidth".format(bw_val))
            return
        self._settings.rf_settings.bandwidth = bandwidth
        self._set_bandwidth = True
        self._maybe_update_rf_settings()

    def _maybe_update_rf_settings(self):
        if self._set_tx_power\
           and self._set_frequencies\
           and self._set_bandwidth:
            self.api_thread.set_rf_settings(self._settings.rf_settings)

    def do_set_frequencies(self, rem):
        """ Configure the frequencies the device will use.

        Usage: set_frequencies CONTROL_FREQ DATA_FREQS....

        All arguments should be frequencies in Hz. The first argument will be used as the control frequency. Subsequent arguments will be data frequencies.
        """
        freqs = rem.split(' ')
        if len(freqs) < 2:
            print("At least one control frequency and one data frequency are required")
            return
        def _check_bands(freq):
            bad = True
            for band in goTenna.constants.BANDS:
                if freq >= band[0] and freq <= band[1]:
                    bad = False
            return bad

        if self.get_device_type() == "900":
            print("This configuration cannot be done for Mesh devices.")
            return
        try:
            control_freq = int(freqs[0])
        except ValueError:
            print("Bad control freq {}".format(freqs[0]))
            return
        if _check_bands(control_freq):
            print("Control freq out of range")
            return
        data_freqs = []
        for idx, freq in enumerate(freqs[1:]):
            try:
                converted_freq = int(freq)
            except ValueError:
                print("Data frequency {}: {} is bad".format(idx, freq))
                return
            if _check_bands(converted_freq):
                print("Data frequency {}: {} is out of range".format(idx, freq))
                return
            data_freqs.append(converted_freq)
        self._settings.rf_settings.control_freqs = [control_freq]
        self._settings.rf_settings.data_freqs = data_freqs
        self._set_frequencies = True
        self._maybe_update_rf_settings()

    def do_list_geo_region(self, rem):
        """ List the available region.

        Usage: list_geo_region
        """
        print("Allowed region:")
        for region in goTenna.constants.GEO_REGION.DICT:
            print("region {} : {}"
                  .format(region, goTenna.constants.GEO_REGION.DICT[region]))

    def do_set_geo_region(self, rem):
        """ Configure the frequencies the device will use.

        Usage: set_geo_region REGION

        Allowed region displayed with list_geo_region.
        """
        if self.get_device_type() == "pro":
            print("This configuration cannot be done for Pro devices.")
            return
        region = int(rem.strip())
        print('region={}'.format(region))
        if not goTenna.constants.GEO_REGION.valid(region):
            print("Invalid region setting {}".format(rem))
            return
        self._set_geo_region = True
        self._settings.geo_settings.region = region
        self.api_thread.set_geo_settings(self._settings.geo_settings)

    def do_can_connect(self, rem):
        """ Return whether a goTenna can connect. For a goTenna to connect, a GID and RF settings must be configured.
        """
        # pylint: disable=unused-argument
        if self.api_thread.gid:
            print("GID: OK")
        else:
            print("GID: Not Set")
        if self._set_tx_power:
            print("PRO - TX Power: OK")
        else:
            print("PRO - TX Power: Not Set")
        if self._set_frequencies:
            print("PRO - Frequencies: OK")
        else:
            print("PRO - Frequencies: Not Set")
        if self._set_bandwidth:
            print("PRO - Bandwidth: OK")
        else:
            print("PRO - Bandwidth: Not Set")
        if self._set_geo_region:
            print("MESH - Geo region: OK")
        else:
            print("MESH - Geo region: Not Set")


    def do_list_groups(self, arg):
        """ List the known groups """
        if not self.api_thread:
            print("The SDK must be configured first.")
            return
        if not self.api_thread.groups:
            print("No known groups.")
            return
        for group in self.api_thread.groups:
            print("Group GID {}: Other members {}"
                  .format(group.gid.gid_val,
                          ', '.join([str(m.gid_val)
                                     for m in group.members
                                     if m != self.api_thread.gid])))

    @staticmethod
    def _version_from_path(path):
        name = os.path.basename(path)
        parts = name.split('.')
        if len(parts) < 3:
            return None
        return (int(parts[0]),
                int(parts[1]),
                int(parts[2]))

    @staticmethod
    def _parse_version(args):
        version_part = args.split('.')
        if len(version_part) < 3:
            return None, args
        return (int(version_part[0]),
                int(version_part[1]),
                int(version_part[2])),\
                '.'.join(version_part[3:])

    @staticmethod
    def _parse_file(args):
        remainder = ''
        if '"' in args:
            parts = args.split('"')
            firmware_file = parts[1]
            remainder = '"'.join(parts[2:])
        else:
            parts = args.split(' ')
            firmware_file = parts[0]
            remainder = ' '.join(parts[1:])
        if not os.path.exists(firmware_file):
            return None, args
        return firmware_file, remainder

    def do_firmware_update(self, args):
        """ Update the device firmware.

        Usage: firmware_update FIRMWARE_FILE [VERSION]
        FIRMWARE_FILE should be the path to a binary firmware. Files or paths containing spaces should be specified in quotes.
        VERSION is an optional dotted version string. The first three dotted segments will determine the version stored in the firmware. If this argument is not passed, the command will try to deduce the version from the filename. If this deduction fails, the command aborts.
        """
        if not self.api_thread.connected:
            print("Device must be connected.")
            return
        firmware_file, rem = self._parse_file(args)
        if not firmware_file:
            print("Cannot find file {}".format(args))
            return
        try:
            version = self._version_from_path(firmware_file)
        except ValueError:
            version = None
        if not version:
            try:
                version, _ = self._parse_version(rem)
            except ValueError:
                print("Version must be 3 numbers separated by '.'")
                return
            if not version:
                print("Version must be specified when not in the filename")
                return
        try:
            open(firmware_file, 'rb').close()
        except (IOError, OSError) as caught_exc:
            print("Cannot open file {}: {}: {}"
                  .format(firmware_file, caught_exc.errno, caught_exc.strerror))
            return
        else:
            print("File {} OK".format(firmware_file))
            print("Beginning firmware update")

        def _callback(correlation_id,
                      success=None, error=None, details=None, results=None):
            # pylint: disable=unused-argument
            if success:
                print("Firmware updated!")
            elif error:
                print("Error updating firmware: {}: {}"
                      .format(details.get('code', 'unknown'),
                              details.get('msg', 'unknown')))
            self.prompt = "goTenna>"

        last_progress = [0]
        print("")
        def _progress_callback(progress, **kwargs):
            percentage = int(progress*100)
            if percentage/10 != last_progress[0]/10:
                last_progress[0] = percentage
                print("FW Update Progress: {: 3}%.".format(percentage))
                if last_progress[0] >= 90:
                    self._awaiting_disconnect_after_fw_update[0] = True
                if last_progress[0] >= 100:
                    print("Device will disconnect and reconnect when update complete.")

        self.prompt = "(updating firmware)"
        self.api_thread.update_firmware(firmware_file, _callback,
                                        _progress_callback, version)

    def do_get_system_info(self, args):
        """ Get system information.

        Usage: get_system_info
        """
        if not self.api_thread.connected:
            print("Device must be connected")
        print(self.api_thread.system_info)

    def send_ser_command(self, command):
        self.serial.write(command)
        sleep(0.5)
        ret = b''
        n = self.serial.in_waiting
        while True:
            if n > 0:
                ret += self.serial.read(n)
            else:
                sleep(.5)
            n = self.serial.in_waiting
            if n == 0:
                break

        return ret

    def do_send_sms(self, args):
        """ Send an SMS message to a particular phone number.

        Usage: send_sms PHONE_NUMBER MESSAGE
        """
        SEND_SMS = b'AT+CMGS="%b"\r'
        SEND_CLOSE = b'\x1A\r'	#sending CTRL-Z

        try:

            # zero or one '+', 9-15 digit phone number,whitespace,message text of 1-n characters
            payload = re.fullmatch(r"([\+]?)([0-9]{9,15})\s(.+)", args)

            phone_number = '+' + payload[2]
            message = payload[3]

            print("Sending SMS to {}".format(phone_number))
            print ("Message: ", message)

            with self.serial_lock:
                # send SMS message
                self.send_ser_command(SEND_SMS % phone_number.encode())
                self.send_ser_command(message.encode()+SEND_CLOSE)

        except serial.SerialTimeoutException:
            print("SerialTimeoutException")

    def print_messages(self, msgs):

        print("Received {} messages:".format(len(msgs)))
        for m in msgs:
            print("Phone Number: {}".format(m['phone_number']))
            print("Received: {}".format(m['received']))
            print("Message: {}".format(m['message']))

    def forward_to_mesh(self, msgs):

        print("Received {} messages:".format(len(msgs)))
        for m in msgs:
            print("\tReceived: {}".format(m['received']))
            print("\tMessage: {}".format(m['message']))
            
            if m['phone_number'] in self.sms_sender_dict:
                mesh_sender_gid = self.sms_sender_dict[m['phone_number']]
                print("\tForwarding message from {} to mesh GID {}:".format(m['phone_number'], mesh_sender_gid))
                args = str(mesh_sender_gid+b' '+m['phone_number']+b' '+m['message'], 'utf-8')
                self.do_send_private(args)
            else:
                print("\tBroadcasting message from {} to mesh:".format(m['phone_number']))
                args = str(m['phone_number']+b' '+m['message'], 'utf-8')
                self.do_send_broadcast(args)

    def do_read_sms(self, args, callback=None):
        """ Read all unread SMS messages received.

        Usage: read_sms
        """
        RETRIEVE_UNREAD = b'AT+CMGL="REC UNREAD"\r'
        msgs = []

        try:
            with self.serial_lock:
                # retrieve all unread SMS messages
                ret = self.send_ser_command(RETRIEVE_UNREAD)

        except serial.SerialTimeoutException:
            print("SerialTimeoutException")

        lines = [line for line in ret.split(b'\r\n') if line.strip() != b'']

        if len(lines) == 0 or lines[0] != RETRIEVE_UNREAD:
            return

        if len(lines) >= 2:
            for n in range(1, len(lines), 2):
                if lines[n] != b'OK':
                    fields = lines[n].split(b",")
                    if len(fields) > 3:
                        phone_number = fields[2].strip(b'"')
                        received = fields[4].strip(b'"')
                        message = lines[n+1]
                        msgs.append({'phone_number':phone_number, 'received':received, 'message':message})

        if len(msgs) > 0:
            if callback != None:
                callback(msgs)
            else:
                print(msgs)

    def do_delete_sms(self, args):
        """ Delete all read and sent SMS messages from phone storage.

        Usage: delete_sms
        """
        DELETE_READ_SENT = b'AT+CMGD=0,2\r' 

        try:
            print("Deleting all read and sent SMS messages.")

            with self.serial_lock:
                # delete all read and sent messages
                self.send_ser_command(DELETE_READ_SENT)

        except serial.SerialTimeoutException:
            print("SerialTimeoutException")

    def do_init_sms(self, args):
        """ Initialize the SMS Modem once when program launched

        Usage: init_sms
        """

        if self.serial == None:
            self.serial = serial.Serial(self.serial_port, self.serial_rate, write_timeout=2)

        OPERATE_SMS_MODE = b'AT+CMGF=1\r'
        ECHO_MODE = b'ATE1\r'
        ENABLE_MODEM = b'AT+CFUN=1\r' 
        SMS_STORAGE = b'AT+CPMS="MT","MT","MT"\r'
        NO_MESSAGE_INDICATORS = b'AT+CNMI=2,0,0,0,0\r'

        try:
            print("Initializing the SMS modem.")

            with self.serial_lock:
                # Set SMS format to text mode
                self.send_ser_command(OPERATE_SMS_MODE)
                # Set echo mode
                self.send_ser_command(ECHO_MODE)
                # Make sure modem is enabled
                self.send_ser_command(ENABLE_MODEM)
                # Store SMS messages received on the modem
                self.send_ser_command(SMS_STORAGE)
                # Disable unsolicited message indicators
                self.send_ser_command(NO_MESSAGE_INDICATORS)

        except serial.SerialTimeoutException:
            print("SerialTimeoutException")

    def do_login_node(self, args):
        url = 'https://users-api-stage-new.gotennamesh.com/v1/users/login'
        headers = {'Content-Type':'application/json'}
        body = json.dumps({"email":self.email, "password":self.password})
        response = requests.post(url, headers=headers, data=body)
        if (response.status_code == 200):
            response_json = json.loads(response.content)
            self.session_token = response_json['session_token']
            self.user_id = response_json['id']
            print("login_node: session_token= " + self.session_token + ", user_id= " + self.user_id)
        else:
            print("login command failed: status code=" + str(response.status_code) + " reason: " + response.reason)

    def do_get_node(self, args):
        print("get_node " + args)
        url = 'https://api-stage.imeshyou.com/nodes/' + args + "?fields=description,use,name,is_ambassador,gateway,user"
        headers = {'Content-Type':'application/json'}
        response = requests.get(url, headers=headers)
        if (response.status_code == 200):
            response_json = json.loads(response.content)
            print(response_json)
        else:
            print("command failed: status code=" + str(response.status_code) + " reason: " + response.reason)

    def do_add_node(self, args):
        url = 'https://api-stage.imeshyou.com/nodes'
        headers = {'Content-Type':'application/json', 'SESSION_TOKEN':self.session_token, 'Authorization':'Bearer '+self.session_token}
        body = json.dumps({'lat':self.latlong[0], 'long':self.latlong[1], 'gotenna_user_id':self.user_id, 'name':self.node_name, 'is_ambassador':False,
            'show_profile':False, 'always_on':False, 'use':['sms gateway'], 'range': self.range, 'description':'', 'user_id':self.user_id, 'gateway':True})
        response = requests.post(url, headers=headers, data=body)
        if (response.status_code == 200):
            response_json = json.loads(response.content)
            self.node_id = response_json['_id']
            print("add_node: node_id=" + self.node_id)
        else:
            print("add_node command failed: status code=" + str(response.status_code) + " reason: " + response.reason)


    def do_update_node(self, args):
        if args != '':
            node_id = args
        else:
            node_id = self.node_id
        print("update_node: node_id= " + str(node_id))
        if node_id is None:
            print("update_node command failed: node_id not defined.")
            return 
        url = 'https://api-stage.imeshyou.com/nodes/' + node_id
        headers = {'Content-Type':'application/json', 'SESSION_TOKEN':self.session_token, 'Authorization':'Bearer '+self.session_token}
        body = json.dumps({'_id':node_id, 'name':self.node_name, 'use':['sms gateway'], 'range':self.range, 'description':'', 'gotenna_user_id':self.user_id, 'gateway':True})
        response = requests.put(url, headers=headers, data=body)
        if (response.status_code == 200):
            response_json = json.loads(response.content)
            self.node_id = response_json['_id']
        else:
            print("update_node command failed: status code=" + str(response.status_code) + " reason: " + response.reason)

    def do_delete_node(self, args):
        if args != '':
            self.node_id = args
        print("delete_node: node_id= " + str(self.node_id))
        if self.node_id is None:
            print("delete_node command failed: node_id not defined.")
            return
        url = 'https://api-stage.imeshyou.com/nodes/' + self.node_id
        headers = {'Content-Type':'application/json', 'SESSION_TOKEN':self.session_token, 'Authorization':'Bearer '+self.session_token}
        response = requests.delete(url, headers=headers)
        if (response.status_code == 200):
            response_json = json.loads(response.content)
            self.node_id = None
        else:
            print("delete_node command failed: status code=" + str(response.status_code) + " reason: " + response.reason)

    def update_imeshyou(self, ):
        self.do_update_node("")
        self.imeshyou_last_update = datetime.now()
        while self.imeshyou_last_update != None:
            if self.imeshyou_last_update + timedelta(hours=1) < datetime.now():
                # refresh last update time of node on imeshyou web site
                self.do_update_node("")
                self.imeshyou_last_update = datetime.now()
            sleep(10)

def run_cli():
    """ The main function of the sample app.

    Instantiates a CLI object and runs it.
    """
    import argparse
    import six

    parser = argparse.ArgumentParser('Run a SMS message goTenna gateway')
    parser.add_argument('--config', type=str, default="mesh_gateway.ini",
                        help='configuration file')
                      
    args = parser.parse_args()  

    config = configparser.ConfigParser()
    config.read(args.config)

    cli_obj = goTennaCLI()
    if config.has_section('gotenna'):
        cli_obj.do_sdk_token(config['gotenna']['sdk_token'])
        cli_obj.do_set_geo_region(config['gotenna']['geo_region'])
        cli_obj.do_set_gid(config['gotenna']['gateway_gid'])

    if config.has_section('sms'):
        cli_obj.serial_port = config['sms']['serial_port']
        cli_obj.serial_rate = config['sms']['serial_rate']

    if config.has_section('imeshyou'):
        cli_obj.email = config['imeshyou']['email']
        cli_obj.password = config['imeshyou']['password']
        cli_obj.do_login_node("")

        cli_obj.latlong = json.loads(config['imeshyou']['latlong'])
        cli_obj.range = config['imeshyou']['range']
        cli_obj.use_tags = json.loads(config['imeshyou']['use_tags'])
        cli_obj.node_id = config['imeshyou']['node_id']

        if cli_obj.node_id == '':
            cli_obj.do_add_node("")
            config['imeshyou']['node_id'] = cli_obj.node_id
            with open(args.config, 'w') as configfile:
                config.write(configfile)
        
        if cli_obj.node_id != '':
            cli_obj.imeshyou_thread = Thread(target = cli_obj.update_imeshyou)
            cli_obj.imeshyou_thread.start()

    try:
        sleep(5)
        cli_obj.cmdloop("Welcome to the SMS Mesh Gateway API sample! "
                        "Press ? for a command list.\n")
    except Exception: # pylint: disable=broad-except
        traceback.print_exc()
        cli_obj.do_quit('')

if __name__ == '__main__':

    run_cli()
