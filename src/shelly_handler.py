#!/usr/bin/env python
#
#  Copyright 2021, 2022 CRS4 - Center for Advanced Studies, Research and
#  Development in Sardinia
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#

"""
Edge Gateway Shelly EM energy monitor handler.
Based on Shelly API: https://shelly-api-docs.shelly.cloud/gen1/
"""

import click
from click_config_file import configuration_option, configobj_provider
from collections import namedtuple
import datetime
import json
import influxdb
from influxdb.exceptions import InfluxDBClientError
import logging
import paho.mqtt.client as mqtt
import paho.mqtt.publish as publish
import signal
import sys


APPLICATION_NAME = 'shelly_handler'
logger = logging.getLogger(APPLICATION_NAME)


TOPIC_LIST = [
    'shellies',
]


# Supresses 'requests' library default logging
logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)


PARAMETERS_MAP = {
    'reactive_power': 'apparentPower',
    'power': 'realPower',
    'total': 'consumedEnergy',
    'voltage': 'voltage'
}


MESSAGE_PARAMETERS = PARAMETERS_MAP.keys()


def cast_to_numeric(value):
    try:
        return float(value)
    except ValueError:
        if isinstance(value, str):
            if value.lower() in ['on', 'true']:
                return int(1)
            elif value.lower() in ['off', 'false']:
                return int(0)
        raise ValueError


class MQTTConnection():
    """Helper class for MQTT connection handling"""

    def __init__(self, host='localhost', port=1883, keepalive=60, logger=None,
                 userdata=None):
        # pylint: disable=too-many-arguments
        self._shelly_broker_host = host
        self._shelly_broker_port = port
        self._keepalive = keepalive
        self._userdata = userdata

        self._logger = logger
        if self._logger is None:
            self._logger = logger.getLoger()

        self._local_client = mqtt.Client(userdata=self._userdata)
        self._local_client.on_connect = self._on_connect
        self._local_client.on_message = self._on_message
        self._local_client.on_disconnect = self._on_disconnect

    def connect(self):
        self._logger.debug(
            "Connecting to Shelly MQTT broker '%s:%d'",
            self._shelly_broker_host, self._shelly_broker_port)
        try:
            self._local_client.connect(
                self._shelly_broker_host,
                self._shelly_broker_port, self._keepalive)
        except Exception as ex:
            self._logger.fatal(
                "Connection to Shelly MQTT broker '%s:%d' failed. "
                "Error was: %s.", self._shelly_broker_host,
                self._shelly_broker_port, str(ex))
            self._logger.fatal("Exiting.")
            sys.exit(-1)

        self._local_client.loop_forever()

    def signal_handler(self, signal, frame):
        self._logger.info("Got signal '{:d}': exiting.".format(signal))
        self._local_client.disconnect()

    def _on_connect(self, client, userdata, flags, rc):
        # pylint: disable=unused-argument,invalid-name
        self._logger.info(
            "Connected to Shelly MQTT broker '%s:%d' with result code %d",
            self._shelly_broker_host, self._shelly_broker_port, rc)

        for _topic in TOPIC_LIST:
            _topic += '/#'

            self._logger.debug("Subscribing to {:s}".format(_topic))

            (result, _) = client.subscribe(_topic)
            if result == mqtt.MQTT_ERR_SUCCESS:
                self._logger.info("Subscribed to {:s}".format(_topic))

    def _on_disconnect(self, client, userdata, rc):
        # pylint: disable=unused-argument,invalid-name
        self._logger.info("Disconnected with result code {:d}".format(rc))

    def _on_message(self, client, userdata, msg):
        # pylint: disable=unused-argument
        _message = msg.payload.decode()
        _timestamp = int(datetime.datetime.now().timestamp())

        self._logger.debug(
            "Received MQTT message - topic: \'{:s}\', message: \'{:s}\'".
            format(msg.topic, _message))

        Measurement = namedtuple('Measurement',
                                 'prefix node type channel field, value')

        _tokens = msg.topic.split('/')

        if len(_tokens) < 4:
            self._logger.warning(
                f"Unhandled message of unknown type '{msg.topic}'")
            return
            # if _tokens[1] == 'announce':
            #     _measurement = None
            #     self._logger.warning(
            #         f"Unhandled message of unknown type '{msg.topic}'")
            # elif _tokens[2] == 'announce':
            #     _measurement = None
            # elif _tokens[2] == 'online':
            #     _measurement = None
            # elif _tokens[2] == 'info':
            #     _measurement = None
        else:
            if _tokens[2] == 'emeter':
                _measurement = Measurement(*_tokens, _message)
            elif _tokens[2] == 'relay':
                _measurement = Measurement(*_tokens, 'status', _message)
            else:
                self._logger.warning(
                    f"Unhandled message of unknown type '{msg.topic}'")
                return

        try:
            _json_data = [{
                "measurement": _measurement.type,
                "tags": {
                    "node": str(_measurement.node),
                    "channel": str(_measurement.channel),
                },
                "time": _timestamp,
                "fields": {
                    _measurement.field: cast_to_numeric(_measurement.value)
                }
            }]
        except Exception as ex:
            self._logger.error(ex)
            return

        v_influxdb_host = userdata['INFLUXDB_HOST']
        v_influxdb_port = userdata['INFLUXDB_PORT']
        v_influxdb_username = userdata['INFLUXDB_USER']
        v_influxdb_password = userdata['INFLUXDB_PASS']
        v_influxdb_database = userdata['INFLUXDB_DB']

        try:
            _client = influxdb.InfluxDBClient(
                host=v_influxdb_host,
                port=v_influxdb_port,
                username=v_influxdb_username,
                password=v_influxdb_password,
                database=v_influxdb_database
            )

            _client.write_points(_json_data, time_precision='s')
            self._logger.debug(
                "Insert data into InfluxDB: {:s}".format(str(_json_data)))
        except InfluxDBClientError as iex:
            self._logger.error(iex)
        except Exception as ex:
            self._logger.error(ex)
        finally:
            _client.close()

        v_internal_broker_host = userdata['MQTT_LOCAL_HOST']
        v_internal_broker_port = userdata['MQTT_LOCAL_PORT']

        if _measurement.field in PARAMETERS_MAP:
            _message = dict()
            _dateObserved = datetime.datetime.fromtimestamp(
                    int(_timestamp), tz=datetime.timezone.utc).isoformat()
            _payload = {
                'timestamp': _timestamp,
                'dateObserved': _dateObserved,
                PARAMETERS_MAP[_measurement.field]: _measurement.value
            }

            _message["payload"] = json.dumps(_payload)
            _message["topic"] = (f"EnergyMonitor/{_measurement.node}"
                                 f".{_measurement.channel}")
            _message['qos'] = 0
            _message['retain'] = False

            try:
                self._logger.debug(
                    ("Sending MQTT message - topic: \'%s\', "
                     "broker: \'%s:%d\', payload: \'%s\'"),
                    _message['topic'], v_internal_broker_host,
                    v_internal_broker_port, _message['payload'])
                publish.multiple([_message], v_internal_broker_host,
                                 v_internal_broker_port)
            # except socket.error:
            except Exception as ex:
                self._logger.warning(ex)


@click.command()
@click.option("--logging-level", envvar="LOGGING_LEVEL",
              type=click.Choice(['DEBUG', 'INFO', 'WARNING',
                                 'ERROR', 'CRITICAL']), default='INFO')
@click.option('--mqtt-local-host', envvar='MQTT_LOCAL_HOST',
              type=str, default='localhost', show_default=True,
              show_envvar=True,
              help=('hostname or address of the local broker'))
@click.option('--mqtt-local-port', envvar='MQTT_LOCAL_PORT',
              type=int, default=1883, show_default=True, show_envvar=True,
              help=('port of the local broker'))
@click.option('--shelly-broker-host', envvar='SHELLY_BROKER_HOST',
              type=str, default='localhost', show_default=True,
              show_envvar=True,
              help=('hostname or address of the Shelly broker'))
@click.option('--shelly-broker-port', envvar='SHELLY_BROKER_PORT',
              type=int, default=1883, show_default=True, show_envvar=True,
              help=('port of the Shelly broker'))
@click.option('--influxdb-host', envvar='INFLUXDB_HOST',
              type=str, default='localhost', show_default=True,
              show_envvar=True,
              help=('hostname or address of the influx database'))
@click.option('--influxdb-port', envvar='INFLUXDB_PORT',
              type=int, default=8086, show_default=True, show_envvar=True,
              help=('port of the influx database'))
@click.option('--influxdb-username', envvar='INFLUXDB_USER',
              type=str, default='', show_default=True, show_envvar=True,
              help=('user of the influx database'))
@click.option('--influxdb-password', envvar='INFLUXDB_PASS',
              type=str, default='', show_default=True, show_envvar=True,
              help=('password of the influx database'))
@click.option('--influxdb-database', envvar='INFLUXDB_DB',
              type=str, default='shelly', show_default=True, show_envvar=True,
              help=('database inside the influx database'))
@configuration_option("--config", "-c", provider=configobj_provider(
                      unrepr=False, section="SHELLY"))
@click.pass_context
def shelly_handler(ctx, mqtt_local_host: str, mqtt_local_port: int,
                   shelly_broker_host: str, shelly_broker_port: int,
                   influxdb_host: str, influxdb_port: int,
                   influxdb_username: str, influxdb_password: str,
                   influxdb_database: str, logging_level) -> None:
    _level = getattr(logging, logging_level)
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt="%Y-%m-%d %H:%M:%S",
        level=_level)

    logger.debug("loggin level set to %s",
                 logging_level)
    logger.debug("local broker is mqtt://%s:%d",
                 mqtt_local_host, mqtt_local_port)

    # Checks the Python Interpeter version
    if sys.version_info < (3, 7):
        logger.fatal("This software requires Python version >= 3.7: exiting.")
        sys.exit(-1)

    _userdata = {
        'MQTT_LOCAL_HOST': mqtt_local_host,
        'MQTT_LOCAL_PORT': mqtt_local_port,
        'INFLUXDB_HOST': influxdb_host,
        'INFLUXDB_PORT': influxdb_port,
        'INFLUXDB_USER': influxdb_username,
        'INFLUXDB_PASS': influxdb_password,
        'INFLUXDB_DB': influxdb_database
    }
    print(_userdata)

    _client = influxdb.InfluxDBClient(
        host=influxdb_host,
        port=influxdb_port,
        username=influxdb_username,
        password=influxdb_password,
        database=influxdb_database
    )

    try:
        _dbs = _client.get_list_database()
        if influxdb_database not in [_d['name'] for _d in _dbs]:
            logger.info(
                "InfluxDB database '{:s}' not found. Creating a new one."
                .format(influxdb_database))
            _client.create_database(influxdb_database)

        _client.close()
    except Exception as ex:
        logger.fatal(
            "Connection to InfluxDB '%s:%d' failed. "
            "Error was: %s.", influxdb_host,
            influxdb_port, str(ex))
        logger.fatal("Exiting.")
        sys.exit(-1)

    connection = MQTTConnection(shelly_broker_host, shelly_broker_port,
                                logger=logger, userdata=_userdata)
    signal.signal(signal.SIGINT, connection.signal_handler)

    connection.connect()


if __name__ == "__main__":
    shelly_handler()

# vim:ts=4:expandtab
