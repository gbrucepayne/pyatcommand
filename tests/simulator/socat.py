#!/usr/bin/env python
"""A basic `socat` modem simulator with predefined responses to AT commands.

DCE (Data Communications Equipment) represents the modem.
DTE (Data Terminal Equipment) represents the computer talking to the modem.

"""

import logging
import json
import os
import subprocess
import threading
import time
import atexit
import string

import serial
from serial.tools.list_ports import comports

BAUDRATE = int(os.getenv('BAUDRATE', '9600'))
DCE = os.getenv('DCE', './simdce')
DTE = os.getenv('DTE', './simdte')
COMMAND_FILE = os.getenv('COMMAND_FILE')
SOCAT_SETUP_DELAY_S = 1
LOOPBACK_INTERVAL_S = 10

VRES_OK = '\r\nOK\r\n'
RES_OK = '0\r'
VRES_ERR = '\r\nERROR\r\n'
RES_ERR = '4\r'

_log = logging.getLogger(__name__)


class UnprintableException(Exception):
    """The decoded character is not printable."""


class SerialBridge:
    def __init__(self, dte: str = DTE, dce: str = DCE, baudrate: int = BAUDRATE) -> None:
        self.dte: str = dte
        self.dce: str = dce
        self.baudrate: int = baudrate
        self._stdout: 'bytes|None' = None
        self._stderr: 'bytes|None' = None
        self._process: subprocess.Popen = None
        self._thread: threading.Thread = None
        atexit.register(self.stop)
    
    def _socat(self):
        # check if port exists else create pty
        params = ',rawer,echo=0'
        if not any(p.device == self.dce for p in comports()):
            dce_params = 'pty' + params + f',link={self.dce}'
        else:
            dce_params = self.dce + params
        if not any(p.device == self.dte for p in comports()):
            dte_params = 'pty' + params + f',link={self.dte}'
        else:
            dte_params = self.dte + params
        cmd = f'socat -d -d {dce_params} {dte_params}'
        _log.debug('Executing: %s', cmd)
        #TODO: revisit -v option?
        self._process = subprocess.Popen(cmd,
                                         stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE,
                                         shell=True)
        self._stdout, self._stderr = self._process.communicate()
        
    def start(self):
        """Start the simulated serial interface."""
        self._thread = threading.Thread(target=self._socat,
                                        name='serial_bridge',
                                        daemon=True)
        self._thread.start()
        time.sleep(SOCAT_SETUP_DELAY_S)
    
    def stop(self):
        self._process.kill()
        if self._stderr:
            _log.error('%s', self._stderr)


class ModemSimulator:
    def __init__(self) -> None:
        self.echo: bool = True
        self.verbose: bool = True
        self.terminator: bytes = '\r'
        self.commands: 'dict[str, dict]' = {}
        self.default_ok: 'list[str]' = ['AT']
        self._running: bool = False
        self._thread: threading.Thread = None
        self._ser: serial.Serial = None
        self.baudrate: int = BAUDRATE
        self._request: str = ''
    
    def start(self, port: str, baudrate: int = BAUDRATE, command_file: str = None):
        if self._running:
            return
        self._running = True
        if command_file:
            try:
                with open(command_file) as f:
                    self.commands = json.load(f)
                #TODO: validate structure
                _log.info('Commands: %s', json.dumps(self.commands))
            except Exception as exc:
                _log.error(exc)
        try:
            self._ser = serial.Serial(port, baudrate)
            self._thread = threading.Thread(target=self._run,
                                            name='modem_simulator',
                                            daemon=True)
            self._thread.start()
            _log.info('Starting modem simulation on %s at %d baud',
                      port, baudrate)
        except Exception as exc:
            _log.error(exc)
    
    def _run(self):
        while self._running:
            if not self._ser.is_open:
                self.stop()
                _log.error('DTE not connected')
                return
            if self._ser.in_waiting > 0:
                b = self._ser.read()
                try:
                    c = b.decode()
                    if not(c in string.printable):
                        raise UnprintableException
                    if self.echo:
                        self._ser.write(b)
                    if c != self.terminator:
                        self._request += c
                        continue
                    _log.debug('Processing command: %s', _debugf(self._request))
                    response = ''
                    if self.commands and self._request in self.commands:
                        res_meta = self.commands[self._request]
                        if isinstance(res_meta, str):
                            response = res_meta
                        elif 'response' in res_meta:
                            response = res_meta['response']
                            if ('delay' in res_meta and
                                isinstance(res_meta['delay'], (float, int))):
                                time.sleep(res_meta['delay'])
                    elif self._request in self.default_ok:
                        response = VRES_OK if self.verbose else RES_OK
                    else:
                        _log.error('Unsupported command: %s', self._request)
                        response = VRES_ERR if self.verbose else RES_ERR
                    if response:
                        _log.debug('Sending response: %s\n', _debugf(response))
                        self._ser.write(response.encode())
                    self._request = ''
                except (UnicodeDecodeError, UnprintableException):
                    _log.error('Bad byte received [%d] - clearing buffer\n',
                               b[0])
                    self._request = ''
    
    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join()
            

def _debugf(debug_str: str) -> str:
    return debug_str.replace('\r', '<cr>').replace('\n', '<lf>')


#TODO: remove
# def socat(dte: str = DTE, dce: str = DCE, debug: bool = False):
#     """Start a socat proxy for a given source to a given target.
    
#     Basically a virtual serial port bridge.
    
#     Args:
#         dte: The name of the DTE interface to create for the computer to connect
#         dce: The name of the DCE interface to create representing the modem
#         debug: If set, prints the output on exit

#     """
#     cmd = (f'socat -d -d -v pty,rawer,echo=0,link={dte}'
#            f' pty,rawer,echo=0,link={dce}')
#     process = subprocess.Popen(cmd,
#                                stdout=subprocess.PIPE,
#                                stderr=subprocess.PIPE,
#                                shell=True)
#     stdout, stderr = process.communicate()
#     # TODO: find a way to print just the tty association, not other stuff...
#     if stdout and debug:
#         print(f'STDOUT: {stdout}')
#     if stderr and debug:
#         print(f'STDERR: {stderr}')


# def simulate(dte_port: str = DTE, echo: bool = True):
#     """Simulates a modem listening and responding to AT commands.
    
#     Args:
#         dte_name: The name of the computer serial port
#         dce_name: The name of the modem serial port
#         echo: Flag indicating if echo is enabled
    
#     """
#     _log.info(">>> Starting modem simulator on %s at %d baud",
#               dte_port, BAUDRATE)
#     supported_commands = 
#     dte = serial.Serial(dte_port, BAUDRATE)
#     try:
#         print(f'>>>> Starting AT simulator bridging modem {dce_name}'
#               f' to {dte_name} at {BAUDRATE} baud')
#         socat_thread = threading.Thread(target=socat,
#                                         args=(dte_name, dce_name),
#                                         daemon=True)
#         socat_thread.start()
#         time.sleep(1)
#         dce = serial.Serial(port=dce_name, baudrate=BAUDRATE)

#         def dce_write(data: str, delay: float = 0):
#             time.sleep(delay)
#             dce.write(data.encode())

#         TERMINATOR = '\r'
#         ok_responses = ['AT', 'ATZ', 'AT&W']
#         verbose_ok = '\r\nOK\r\n'
#         verbose_error = '\r\nERROR\r\n'
#         last_error = None
#         rx_data: str = ''
#         while dce.is_open:
#             if dce.in_waiting > 0:
#                 char = dce.read(dce.in_waiting).decode()
#                 if echo:
#                     dce_write(char)
#                 rx_data += char
#                 if char == TERMINATOR:
#                     rx_data = rx_data.strip()
#                     if rx_data.upper() == 'QUIT':
#                         dce_write('Exiting...')
#                         break
#                     elif rx_data.upper() in ok_responses:
#                         dce_write(verbose_ok)
#                     else:
#                         # TODO: %CRC, %OFF, %TRK, %UTC
#                         logging.warning(f'AT command {rx_data} unsupported')
#                         dce_write(verbose_error)
#                     rx_data = ''
#     finally:
#         if dce and dce.is_open:
#             dce.close()
#         socat_thread.join()
#         print('<<<< Exiting AT simulator')


def loopback_test(ser: serial.Serial) -> bool:
    if not ser.is_open:
        return
    test_command = 'AT'
    timeout = 5
    success = False
    response: str = ''
    _log.info('Sending test command: %s', test_command)
    ser.write(f'{test_command}\r'.encode())
    start_time = time.time()
    while (not success and time.time() - start_time < timeout):
        if ser.in_waiting > 0:
            b = ser.read()
            try:
                c = b.decode()
                response += c
                if response.endswith((VRES_OK, RES_OK, VRES_ERR, RES_ERR)):
                    success = True
            except Exception as exc:
                _log.exception(exc)
    if success:
        _log.info('Test command success!')
    else:
        _log.warning('Timed out waiting for response')
    return success


if __name__ == '__main__':
    print('>>>> Starting simulator')
    logging.basicConfig(level=logging.DEBUG)
    bridge = None
    simulator = None
    listen_on = DTE
    loopback = False
    try:
        simulator = ModemSimulator()
        if (DCE.endswith('simdce') and DTE.endswith('simdte')):
            bridge = SerialBridge(DTE, DCE, BAUDRATE)
            bridge.start()
            loopback = True
            listen_on = DCE
        simulator.start(listen_on, BAUDRATE, COMMAND_FILE)
        if loopback:
            dte = serial.Serial(DTE, BAUDRATE) 
        while True:
            if loopback:
                success = loopback_test(dte)
                time.sleep(LOOPBACK_INTERVAL_S)
    except KeyboardInterrupt:
        _log.info('Keyboard Interrupt')
    except Exception as exc:
        _log.exception(exc)
    finally:
        print('<<<< Exiting AT simulator')
