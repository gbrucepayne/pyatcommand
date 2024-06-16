"""Example of using AT Client to communicate with a modem.
"""
import logging
import os
import time

from pyatcommand import AtClient, AtErrorCode

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

logger = logging.getLogger('atclient')

command_interval = 10   # seconds


def basic_example():
    urc_count = 0
    command_count = 0
    serial_port = os.getenv('SERIAL_PORT', '/dev/ttyUSB0')
    modem = AtClient()
    modem.connect(serial_port)
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
                elif time.time() - last_cmd >= command_interval:
                    command_count += 1
                    logger.info('Command %d', command_count)
                    rc = modem.send_at_command('ATI', timeout=-1)
                    last_cmd = time.time()
                    if rc != AtErrorCode.OK:
                        logger.warning('Problem sending basic command (%s)',
                                       modem.last_error_code(True))
                    if modem.is_response_ready():
                        response = modem.get_response()
                        if response:
                            logger.info('Cleaned response: %s', response)
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    basic_example()
