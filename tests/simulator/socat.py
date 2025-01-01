#!/usr/bin/env python
"""A basic `socat` modem simulator with predefined responses to AT commands.

DCE (Data Communications Equipment) represents the modem.
DTE (Data Terminal Equipment) represents the computer talking to the modem.

"""

import logging
import json
import os
import signal
import subprocess
import threading
import time
import atexit
import string
import re

import serial
from serial.tools.list_ports import comports

BAUDRATE = int(os.getenv('BAUDRATE', '9600'))
DCE = os.getenv('DCE', './simdce')
DTE = os.getenv('DTE', './simdte')
COMMAND_FILE = os.getenv('COMMAND_FILE', './tests/simulator/commands.json')
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
    """A `socat` bridge between 2 physical and/or virtual serial ports."""
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
                                         shell=True,
                                         preexec_fn=os.setsid)
        self._stdout, self._stderr = self._process.communicate()
        
    def start(self):
        """Start the simulated serial interface."""
        self._thread = threading.Thread(target=self._socat,
                                        name='serial_bridge',
                                        daemon=True)
        self._thread.start()
        time.sleep(SOCAT_SETUP_DELAY_S)
    
    def stop(self):
        try:
            # self._process.kill()
            os.killpg(os.getpgid(self._process.pid), signal.SIGTERM)
            if self._stderr:
                _log.error('%s', self._stderr)
        except Exception as err:
            _log.error('Serial bridge stop: %s', err)


class ModemSimulator:
    """A simulator for an AT command driven modem."""
    def __init__(self) -> None:
        self.echo: bool = True
        self.verbose: bool = True
        self.terminator: bytes = '\r'
        self.commands: 'dict[str, dict]' = {}
        self.default_ok: 'list[str]' = ['AT']
        self._running: bool = False
        self._thread: threading.Thread = None
        self._ser: serial.Serial = None
        self._baudrate: int = BAUDRATE
        self._request: str = ''
    
    @property
    def baudrate(self) -> int:
        return self._baudrate
    
    @baudrate.setter
    def baudrate(self, value: int):
        if not isinstance(value, int) or value <= 0:
            raise ValueError('Invalid baudrate')
        self._baudrate = value
        if self._ser and self._ser.baudrate != self._baudrate:
            _log.warning('Changing active serial baudrate to %d', self._baudrate)
            self._ser.baudrate = self._baudrate
    
    def start(self,
              port: str = DCE,
              baudrate: int = None,
              command_file: str = None,
              ):
        if self._running:
            return
        self._running = True
        if isinstance(baudrate, int):
            self.baudrate = baudrate
        if command_file:
            try:
                with open(command_file) as f:
                    self.commands = json.load(f)
                #TODO: validate structure
                _log.info('Using commands: %s', json.dumps(self.commands))
            except Exception as exc:
                _log.error(exc)
        try:
            self._ser = serial.Serial(port, self.baudrate)
            self._thread = threading.Thread(target=self._run,
                                            name='modem_simulator',
                                            daemon=True)
            self._thread.start()
            _log.info('Starting modem simulation on %s at %d baud',
                      port, self.baudrate)
        except Exception as exc:
            _log.error(exc)
    
    def _run(self):
        while self._running:
            if self._ser and not self._ser.is_open:
                self.stop()
                _log.error('DTE not connected')
                return
            if self._ser and self._ser.in_waiting > 0:
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
                    if self._request.lower().startswith('ATE'):
                        self.echo = self._request.endswith('1')
                        response = VRES_OK if self.verbose else RES_OK
                    elif self._request.lower().startswith('ATV'):
                        self.verbose = self._request.endswith('1')
                        response = VRES_OK if self.verbose else RES_OK
                    elif self.commands and self._request in self.commands:
                        _log.debug('Processing custom response')
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
                    elif 'BAD_BYTE' in self._request:
                        self._ser.write(bytes([255]))
                    else:
                        _log.error('Unsupported command: %s', self._request)
                        response = VRES_ERR if self.verbose else RES_ERR
                    if response:
                        if not self.verbose:
                            pattern = r'\r\n.*?\r\n'
                            lines = re.findall(pattern, response, re.DOTALL)
                            lines[-1] = lines[-1].strip() + '\r'
                        _log.debug('Sending %s response: %s',
                                   _debugf(self._request), _debugf(response))
                        self._ser.write(response.encode())
                    self._request = ''
                except (UnicodeDecodeError, UnprintableException):
                    _log.error('Bad byte received [%d] - clearing buffer\n',
                               b[0])
                    self._request = ''
    
    def inject_urc(self, urc: str, v0_header: str = '\r\n'):
        """Inject an unsolicited response code."""
        if not isinstance(urc, str) or not urc:
            _log.error('Invalid URC')
            return
        if self.verbose:
            urc = f'\r\n{urc}\r\n'
        else:
            urc = f'{v0_header}{urc}\r\n'
        _log.debug('Sending URC: %s', _debugf(urc))
        self._ser.write(urc.encode())
        self._ser.flush()
    
    def multi_urc(self, urcs: 'list[str]', v0_header: str = '\r\n'):
        """Inject multiple unsolicited outputs."""
        if self.verbose:
            chained = '\r\n'.join(f'\r\n{urc}' for urc in urcs)
        else:
            chained = '\r\n'.join(f'{v0_header}{urc}' for urc in urcs)
        chained += '\r\n'
        _log.debug('Sending chained URCS: %s', _debugf(chained))
        self._ser.write(chained.encode())
        self._ser.flush()
    
    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join()
            

def _debugf(debug_str: str) -> str:
    return debug_str.replace('\r', '<cr>').replace('\n', '<lf>')


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
