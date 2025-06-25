"""Example of using AT Client to communicate with a modem.
"""
import logging
import os
import time

from pyatcommand import AtClient
from pyatcommand.common import dprint

LOG_LEVEL = logging.DEBUG
logfmt = ('%(asctime)s,[%(levelname)s],(%(threadName)s)'
          ',%(module)s.%(funcName)s:%(lineno)d'
          ',%(message)s')
console = logging.StreamHandler()
console.setLevel(LOG_LEVEL)

logging.basicConfig(level=LOG_LEVEL,
                    format=logfmt,
                    datefmt='%Y-%m-%dT%H:%M:%S',
                    handlers=[logging.FileHandler('./logs/client.log', mode='w'),
                              console])

logger = logging.getLogger()

TEST_COMMAND = 'AT'
TEST_COMMAND_INTERVAL = 10   # seconds


def basic_example():
    urc_count = 0
    command_count = 0
    serial_port = os.getenv('SERIAL_PORT', '/dev/ttyUSB0')
    baudrate = int(os.getenv('SERIAL_BAUDRATE', '9600'))
    modem = AtClient()
    modem.connect(port=serial_port, baudrate=baudrate)
    if modem.is_connected():
        logger.info('Modem connected on %s', serial_port)
        last_cmd = time.time()
        try:
            while True:
                if modem.check_urc():
                    if not modem.is_response_ready():
                        logger.warning('Unable to print URC')
                        continue
                    urc_count += 1
                    urc = modem.get_response()
                    logger.info('Got URC: %s', urc)
                elif time.time() - last_cmd >= TEST_COMMAND_INTERVAL:
                    command_count += 1
                    logger.info('Sending command # %d', command_count)
                    res = modem.send_command(TEST_COMMAND)
                    last_cmd = time.time()
                    if not res.ok:
                        logger.warning('Problem sending basic command (%s)',
                                       dprint(TEST_COMMAND))
                    elif res.info:
                        logger.info('Cleaned response: %s', dprint(res.info))
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    basic_example()
