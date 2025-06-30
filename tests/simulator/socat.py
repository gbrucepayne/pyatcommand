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
        self.terminator: str = '\r'
        self.commands: 'dict[str, dict]' = {}
        self.default_ok: 'list[str]' = ['AT']
        self._running: bool = False
        self._thread: threading.Thread = None
        self._ser: serial.Serial = None
        self._baudrate: int = BAUDRATE
        self._request: str = ''
        self.intermediate_pause: bool = False
        self._data_mode: bool = False
        self.data_mode_data = bytearray()
        self._last_data_mode_rx_time: float = 0
        self._data_mode_exit: 'str|None' = None
        self._data_mode_exit_delay: float = 0
        self._data_mode_exit_res: 'str|None' = None
    
    @property
    def data_mode(self) -> bool:
        return self._data_mode
    
    @data_mode.setter
    def data_mode(self, value: bool):
        if not isinstance(value, bool):
            raise ValueError('Data mode must be boolean')
        _log.debug('%sing data mode', 'Enter' if value else 'Exit')
        self._data_mode = value

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
            while self._ser and self._ser.in_waiting > 0:
                if self.data_mode:
                    rx_data = self._ser.read(self._ser.in_waiting)
                    if rx_data == self._data_mode_exit.encode():
                        if self._last_data_mode_rx_time > 0:
                            rx_delay = time.time() - self._last_data_mode_rx_time
                        else:
                            rx_delay = 0
                        if (rx_delay >= self._data_mode_exit_delay or
                            self._last_data_mode_rx_time == 0):
                            _log.debug('Received exit sequence: %s after %0.1fs',
                                       rx_data, rx_delay)
                            self.data_mode = False
                            self._data_mode_exit = None
                            self._data_mode_exit_delay = 0
                            self._last_data_mode_rx_time = 0
                    else:
                        self.data_mode_data += rx_data
                        self._last_data_mode_rx_time = time.time()
                        _log.debug('Data mode received %s',
                                   _debugf(self.data_mode_data.decode()))
                        if self._data_mode_exit == '<auto>':
                            _log.debug('Auto-exit data mode')
                            self.data_mode = False
                        else:
                            continue
                    b = None
                else:
                    b = self._ser.read()
                try:
                    c = b.decode() if b else self.terminator   # set terminator for data_mode exit case
                    if c not in string.printable:
                        raise UnprintableException
                    if c != self.terminator:
                        self._request += c
                        continue
                    if self._request:
                        _log.debug('Processing command: %s', _debugf(self._request))
                        echo = self._request + self.terminator if self.echo else ''
                        intermediate_response = ''
                        data_mode_recv: 'bytes|None' = None
                        response = ''
                        response_delay = 0
                        data_delay = 0
                        if self._request.upper() == 'AT':
                            response = VRES_OK if self.verbose else RES_OK
                        elif self._request.upper().startswith('ATE'):
                            self.echo = self._request.endswith('1')
                            response = VRES_OK if self.verbose else RES_OK
                        elif self._request.upper().startswith('ATV'):
                            self.verbose = self._request.endswith('1')
                            _log.debug('Verbose %sabled', 'en' if self.verbose else 'dis')
                            response = VRES_OK if self.verbose else RES_OK
                        elif self.commands and self._request in self.commands:
                            _log.debug('Processing custom response')
                            responses = {'intermediateResponse', 'response', 'recvData'}
                            res_meta = self.commands.get(self._request)
                            if isinstance(res_meta, str):
                                response = res_meta
                            elif any(k in res_meta for k in responses):
                                intermediate_response: str = res_meta.get('intermediateResponse', '')
                                recv_data_str = res_meta.get('recvData')
                                if isinstance(recv_data_str, str):
                                    data_mode_recv = recv_data_str.encode()
                                data_delay = res_meta.get('dataDelay') or data_delay
                                if res_meta.get('enterDataMode') is True:
                                    _log.debug('Command triggers data mode')
                                    self.data_mode = True
                                    self._data_mode_exit = res_meta.get('exitDataMode')
                                    self._data_mode_exit_res = res_meta.get('exitResponse')
                                    if (not self._data_mode_exit or
                                        not self._data_mode_exit_res):
                                        raise ValueError('Data mode exit not defined'
                                                        f' for {res_meta}')
                                response: str = res_meta.get('response') or self._data_mode_exit_res
                                if not response and not self.data_mode:
                                    raise ValueError('Invalid response definition')
                                if res_meta.get('hasEcho') is True:
                                    echo = ''
                                response_delay = res_meta.get('delay') or response_delay
                        elif self._request in self.default_ok:
                            response = VRES_OK if self.verbose else RES_OK
                        else:
                            _log.error('Unsupported command: %s', self._request)
                            response = VRES_ERR if self.verbose else RES_ERR
                    elif self._data_mode_exit_res:
                        response = self._data_mode_exit_res
                        self._data_mode_exit_res = None
                    if echo:
                        _log.debug('Sending echo: %s', _debugf(echo))
                        self._ser.write(echo.encode())
                        echo = ''
                    if intermediate_response:
                        _log.info('Sending intermediate response to %s: %s',
                                  _debugf(self._request),
                                  _debugf(intermediate_response))
                        self._ser.write(intermediate_response.encode())
                        self.intermediate_pause = res_meta.get('intermediatePause', False)
                        notify = self.intermediate_pause
                        if notify:
                            _log.warning('Paused waiting to reset intermediate_pause')
                        while self.intermediate_pause:
                            time.sleep(0.5)
                        if notify:
                            _log.debug('Intermediate pause completed')
                    if response:
                        if response_delay:
                            _log.debug('Delaying response %0.1f seconds', response_delay)
                        time.sleep(response_delay)                            
                        if (isinstance(data_mode_recv, bytes) and
                            intermediate_response):
                            if not self.data_mode:
                                self.data_mode = True
                            if data_delay:
                                _log.debug('Delaying intermediate data %0.1f s',
                                           data_delay)
                            time.sleep(data_delay)
                            _log.debug('Sending received %d bytes intermediate data mode',
                                       len(data_mode_recv))
                            self._ser.write(data_mode_recv)
                            self._ser.flush()
                            time.sleep(0.1)
                            data_mode_recv = None   # clear to avoid resending
                            self.data_mode = False
                        if not self.verbose:
                            pattern = r'\r\n.*?\r\n'
                            lines = re.findall(pattern, response, re.DOTALL)
                            if lines:
                                for i, line in enumerate(lines):
                                    lines[i] = line.replace('\r\n', '', 1)
                                    if i == len(lines) - 1:
                                        lines[i] = RES_OK if 'OK' in line else RES_ERR
                                response = ''.join(lines)
                        to_write = response.encode()
                        if 'BAD_BYTE' in self._request:
                            # since we can't store escaped non-printable in commands.json
                            bad_byte = 0xFF
                            to_write = bytearray(to_write)
                            position = self._request[-2]
                            if position == 'B':
                                bad_byte_offset = 0
                            elif position == 'M':
                                bad_byte_offset = int(len(to_write) / 2)
                            else:
                                bad_byte_offset = len(to_write) - 1
                            to_write.insert(bad_byte_offset, bad_byte)
                        _log.debug('Sending response to %s: %s',
                                   _debugf(self._request or 'data mode exit'),
                                   _debugf(to_write.decode(errors='backslashreplace')))
                        self._ser.write(to_write)
                    if (data_mode_recv):
                        if not self.data_mode:
                            self.data_mode = True
                        if data_delay:
                            _log.debug('Delaying post-response data %0.1f s',
                                       data_delay)
                        time.sleep(data_delay)
                        _log.debug('Sending received %d bytes post-command data mode',
                                    len(data_mode_recv))
                        self._ser.write(data_mode_recv)
                        self._ser.flush()
                        data_mode_recv = None
                        data_delay = 0
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
