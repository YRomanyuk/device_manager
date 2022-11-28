#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import atexit
import time
import signal
import asyncio
from pathlib import PurePosixPath
from concurrent import futures
from functools import partial
from threading import current_thread, Lock
import paho.mqtt.client as mosquitto
from mqttrpc import client as rpcclient
from mqttrpc.manager import AMQTTRPCResponseManager
from mqttrpc.protocol import MQTTRPC10Response
from jsonrpc.exceptions import JSONRPCServerError
from wb_modbus import minimalmodbus, instruments
from . import logger, TOPIC_HEADER


STATE_PUBLISH_QUEUE = asyncio.Queue()


def get_topic_path(*args):
    ret = PurePosixPath(TOPIC_HEADER, *[str(arg) for arg in args])
    return str(ret)


class Singleton(type):
    _instances = {}
    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            cls._instances[cls] = super(Singleton, cls).__call__(*args, **kwargs)
        return cls._instances[cls]


class MQTTConnManager(metaclass=Singleton):  # TODO: split to common lib
    _MQTT_CONNECTIONS = {}
    _CLIENT_NAME = "wb-device-manager"

    DEFAULT_MQTT_HOST = "127.0.0.1"
    DEFAULT_MQTT_PORT_STR = "1883"

    @property
    def mqtt_connections(self):
        return type(self)._MQTT_CONNECTIONS

    def parse_mqtt_addr(self, hostport_str=""):
        host, port = hostport_str.split(":", 1)
        return host or self.DEFAULT_MQTT_HOST, int(port or self.DEFAULT_MQTT_PORT_STR, 0)

    def close_mqtt(self, hostport_str):
        client = self.mqtt_connections.get(hostport_str)

        if client:
            client.loop_stop()
            client.disconnect()
            self.mqtt_connections.pop(hostport_str)
            logger.info("Mqtt: close %s", hostport_str)
        else:
            logger.warning("Mqtt connection %s not found in active ones!", hostport_str)

    def get_mqtt_connection(self, hostport_str=""):
        hostport_str = hostport_str or "%s:%s" % (self.DEFAULT_MQTT_HOST, self.DEFAULT_MQTT_PORT_STR)
        logger.debug("Looking for open mqtt connection for: %s", hostport_str)
        client = self.mqtt_connections.get(hostport_str)

        if client:
            logger.debug("Found")
            return client
        else:
            _host, _port = self.parse_mqtt_addr(hostport_str)
            try:
                client = mosquitto.Client(self._CLIENT_NAME)
                # client.enable_logger(logger)
                logger.info("New mqtt connection; host: %s; port: %d", _host, _port)
                client.connect(_host, _port)
                client.loop_start()
                self.mqtt_connections.update({hostport_str : client})
                return client
            finally:
                logger.info("Registered to atexit hook: close %s", hostport_str)
                atexit.register(lambda: self.close_mqtt(hostport_str))


class RPCResultFuture(asyncio.Future):
    """
    an rpc-call-result obj:
        - is future;
        - supposed to be filled from another thread (on_message callback)
        - compatible with mqttrpc api
    """

    def set_result(self, result):
        if result is not None:
            self._loop.call_soon_threadsafe(partial(super().set_result, result))

    def set_exception(self, exception):
        self._loop.call_soon_threadsafe(partial(super().set_exception, exception))


class SRPCClient(rpcclient.TMQTTRPCClient, metaclass=Singleton):
    """
    Stores internal future-like objs (with rpc-call result), filled from outer on_mqtt_message callback
    """
    async def make_rpc_call(self, driver, service, method, params, timeout=10):
        logger.debug("RPC Client -> %s (rpc timeout: %.2fs)", params, timeout)
        response_f = self.call_async(
            driver,
            service,
            method,
            params,
            result_future=RPCResultFuture
            )
        response = await asyncio.wait_for(response_f, timeout)
        logger.debug("RPC Client <- %s", response)
        return response


class AsyncModbusInstrument(instruments.SerialRPCBackendInstrument):
    """
    Generic minimalmodbus instrument's logic with mqtt-rpc to wb-mqtt-serial as transport
    (instead of pyserial)
    """

    def __init__(self, port, slaveaddress, **kwargs):
        super().__init__(port, slaveaddress, **kwargs)
        mqtt_conn = MQTTConnManager().get_mqtt_connection(hostport_str=self.broker_addr)
        self.rpc_client = SRPCClient(mqtt_conn)
        self.serial.timeout = kwargs.get("response_timeout", 0.5)

    async def _communicate(self, request, number_of_bytes_to_read):
        minimalmodbus._check_string(request, minlength=1, description="request")
        minimalmodbus._check_int(number_of_bytes_to_read)

        rpc_request = {
            "response_size": number_of_bytes_to_read,
            "format": "HEX",
            "msg": minimalmodbus._hexencode(request),
            "response_timeout": round(self.serial.timeout * 1E3),
            "path": self.serial.port,  # TODO: support modbus tcp in minimalmodbus
            "baud_rate" : self.serial.SERIAL_SETTINGS["baudrate"],
            "parity" : self.serial.SERIAL_SETTINGS["parity"],
            "stop_bits" : self.serial.SERIAL_SETTINGS["stopbits"],
            "data_bits" : 8,
        }

        rpc_call_timeout = 10
        try:
            response = await self.rpc_client.make_rpc_call(
                driver="wb-mqtt-serial",
                service="port",
                method="Load",
                params=rpc_request,
                timeout=rpc_call_timeout
                )
        except rpcclient.MQTTRPCError as e:
            reraise_err = minimalmodbus.NoResponseError if e.code == self.RPC_ERR_STATES["REQUEST_HANDLING"] else rpcclient.MQTTRPCError
            raise reraise_err from e
        else:
            return minimalmodbus._hexdecode(str(response.get("response", "")))


class MQTTRPCAlreadyProcessingError(JSONRPCServerError):
    CODE = -33100
    MESSAGE = "Task is already executing."


class MQTTRPCMaxTasksProcessingError(JSONRPCServerError):
    CODE = -33200
    MESSAGE = "Max number of tasks are processing! Try again later."


class MQTTServer:
    _NOW_PROCESSING = []
    MAX_CONCURRENT_TASKS = 10  # TODO: make a performance research

    def __init__(self, methods_dispatcher, state_publish_topic, hostport_str=""):
        self.hostport_str = hostport_str
        self.methods_dispatcher = methods_dispatcher
        self.state_publish_topic = state_publish_topic

        self.connection = MQTTConnManager().get_mqtt_connection(self.hostport_str)

        self.asyncio_loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            self.asyncio_loop.add_signal_handler(sig, lambda: self.asyncio_loop.stop())
        self.asyncio_loop.set_debug(True)

        self.rpc_client = SRPCClient(self.connection)

    @property
    def now_processing(self):
        return type(self)._NOW_PROCESSING

    def add_to_processing(self, mqtt_message):
        self.now_processing.append((mqtt_message.topic, mqtt_message.payload))

    def remove_from_processing(self, mqtt_message):
        self.now_processing.remove((mqtt_message.topic, mqtt_message.payload))

    def is_processing(self, mqtt_message):
        return (mqtt_message.topic, mqtt_message.payload) in self.now_processing

    def _subscribe(self):
        logger.debug("Subscribing to: %s", str(self.methods_dispatcher.keys()))
        for service, method in self.methods_dispatcher.keys():
            topic_str = get_topic_path(service, method)
            self.connection.publish(topic_str, "1", retain=True)
            topic_str += "/+"
            self.connection.subscribe(topic_str)
            logger.debug("Subscribed: %s", topic_str)

    def _on_mqtt_message(self, _client, _userdata, message):
        if mosquitto.topic_matches_sub('/rpc/v1/+/+/+/%s/reply' % self.rpc_client.rpc_client_id, message.topic):
            self.rpc_client.on_mqtt_message(None, None, message)  # reply from mqtt client; filling payload

        else:  # requests to a server
            if self.is_processing(message):
                logger.warning("'%s' is already processing!", message.topic)
                response = MQTTRPC10Response(error=MQTTRPCAlreadyProcessingError()._data)
                self.reply(message, response.json)
            elif len(self.now_processing) < self.MAX_CONCURRENT_TASKS:
                self.add_to_processing(message)
                asyncio.run_coroutine_threadsafe(self.run_async(message), self.asyncio_loop)
            else:
                logger.warning("Max number of tasks (%d) is running already", len(self.now_processing))
                logger.warning("Doing nothing for '%s'", message.topic)
                response = MQTTRPC10Response(error=MQTTRPCMaxTasksProcessingError()._data)
                self.reply(message, response.json)

    def reply(self, message, payload):
        topic = message.topic + "/reply"
        self.connection.publish(topic, payload, False)

    async def run_async(self, message):
        parts = message.topic.split("/")  # TODO: re?
        service_id, method_id = parts[4], parts[5]

        _now = time.time()
        ret = await AMQTTRPCResponseManager.handle(  # wraps any exception into json-rpc
            message.payload,
            service_id,
            method_id,
            self.methods_dispatcher
            )
        _done = time.time()
        logger.info("Processing '%s' took %.2fs", message.topic, _done - _now)
        self.reply(message, ret.json)
        self.remove_from_processing(message)

    async def publish_overall_state(self):
        while True:
            state_json = await STATE_PUBLISH_QUEUE.get()
            self.connection.publish(self.state_publish_topic, state_json, retain=True)

    def setup(self):
        self._subscribe()
        self.connection.on_message = self._on_mqtt_message
        logger.debug("Binded 'on_message' callback")
        self.asyncio_loop.create_task(self.publish_overall_state())

    def loop(self):
        self.asyncio_loop.run_forever()
