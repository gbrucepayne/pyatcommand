#!/usr/bin/env python
"""A basic modem simulator with predefined responses to AT commands.

DCE (Data Communications Equipment) represents the modem.
DTE (Data Terminal Equipment) represents the computer talking to the modem.

References a JSON file structured as:
```
{
    "AT+COMMAND=X": {
        "response": "\r\nOK\r\n",
        "delay": null,
        "hasEcho": false,
        "intermediateResponse": null,
        "intermediatePause": false,
        "dataModeEntry": false,
        "dataReply": null,
        "dataDelay": null,
        "dataModeExitSequence": "<auto>",
        "dataExitDelay": null,
        "dataModeExitResponse": null
    }
}
```
Where:
- response (string) may include information responses and the final result code
- delay (float) adds an optional delay to the response
- hasEcho (boolean) indicates if response has the echo included
- intermediateResponse (string) adds an intermediate before response or
dataModeExitResponse, typically a pause for some input or output such as data mode
- dataModeEntry (boolean|string) sends the string to the DTE before entering
data mode, if true then no prompt is sent to the DTE
- dataModeExitSequence (string) indicates an ASCII sequence that triggers exit
from data mode, or the reserved <auto> means that data mode is exited when
the data is complete or times out
- dataReply (string) sends a response in data mode which encodes the ASCII
string to send back to the DTE after entering data mode
- dataExitDelay (float) adds delay between data mode exit and AT command mode
- dataModeExitResponse (string) final AT response sent after exiting data mode.
May be in addition to response in some cases.

"""

import logging
import json
import os
# import signal
# import subprocess
import threading
import time
# import atexit
import string
import re
from typing import Callable, Optional, Union, Literal

import serial
# from serial.tools.list_ports import comports
from pyatcommand import xmodem_bytes_handler
from pyatcommand.common import dprint

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


# class SerialBridge:
#     """A `socat` bridge between 2 physical and/or virtual serial ports."""
#     def __init__(self, dte: str = DTE, dce: str = DCE, baudrate: int = BAUDRATE) -> None:
#         self.dte: str = dte
#         self.dce: str = dce
#         self.baudrate: int = baudrate
#         self._stdout: 'bytes|None' = None
#         self._stderr: 'bytes|None' = None
#         self._process: subprocess.Popen = None
#         self._thread: threading.Thread = None
#         atexit.register(self.stop)
    
#     def _socat(self):
#         # check if port exists else create pty
#         params = ',rawer,echo=0'
#         if not any(p.device == self.dce for p in comports()):
#             dce_params = 'pty' + params + f',link={self.dce}'
#         else:
#             dce_params = self.dce + params
#         if not any(p.device == self.dte for p in comports()):
#             dte_params = 'pty' + params + f',link={self.dte}'
#         else:
#             dte_params = self.dte + params
#         cmd = f'socat -d -d {dce_params} {dte_params}'
#         _log.debug('Executing: %s', cmd)
#         #TODO: revisit -v option?
#         self._process = subprocess.Popen(cmd,
#                                          stdout=subprocess.PIPE,
#                                          stderr=subprocess.PIPE,
#                                          shell=True,
#                                          preexec_fn=os.setsid)
#         self._stdout, self._stderr = self._process.communicate()
        
#     def start(self):
#         """Start the simulated serial interface."""
#         self._thread = threading.Thread(target=self._socat,
#                                         name='serial_bridge',
#                                         daemon=True)
#         self._thread.start()
#         time.sleep(SOCAT_SETUP_DELAY_S)
    
#     def stop(self):
#         try:
#             # self._process.kill()
#             os.killpg(os.getpgid(self._process.pid), signal.SIGTERM)
#             if self._stderr:
#                 _log.error('%s', self._stderr)
#         except Exception as err:
#             _log.error('Serial bridge stop: %s', err)


class ModemSimulator:
    """A simulator for an AT command driven modem."""
    def __init__(self) -> None:
        self.echo: bool = True
        self.verbose: bool = True
        self.terminator: str = '\r'
        self.commands: dict[str, dict[str, str]] = {}
        self._running: bool = False
        self._thread: threading.Thread = None
        self._ser: serial.Serial = None
        self._baudrate: int = BAUDRATE
        self._request: str = ''
        self.intermediate_pause: bool = False
        self._data_mode: bool = False
        self.data_mode_data = bytearray()
        self._last_data_mode_rx_time: float = 0
        self._data_mode_exit: Union[str, None] = None
        self._data_mode_exit_start: float = 0
        self._data_mode_exit_match_idx: int = 0
        self._data_mode_exit_delay: float = 0
        self._data_mode_exit_res: 'str|None' = None
        self._binary_handler: Optional[Callable[[bytes], None]] = None
    
    @property
    def data_mode(self) -> bool:
        return self._data_mode
    
    @data_mode.setter
    def data_mode(self, value: bool):
        if not isinstance(value, bool):
            raise ValueError('Data mode must be boolean')
        if self._data_mode != value:
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
              command_file: str = COMMAND_FILE,
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
                _log.info('Parsed %d custom commands', len(self.commands.keys()))
            except Exception as exc:
                _log.error(exc)
        try:
            self._ser = serial.Serial(port, self.baudrate, timeout=0.1)
            self._thread = threading.Thread(target=self._run,
                                            name='modem_simulator',
                                            daemon=True)
            self._thread.start()
            _log.info('Starting modem simulation on %s at %d baud',
                      port, self.baudrate)
        except Exception as exc:
            _log.error(exc)
    
    def _reset_data_mode_state(self):
        self.data_mode = False
        self._data_mode_exit = None
        self._data_mode_exit_delay = 0
        self._data_mode_exit_start = 0
        self._data_mode_exit_match_idx = 0
        self._last_data_mode_rx_time = 0
    
    def _handle_data_mode_rx(self):
        now = time.time()
        rx_data = self._ser.read(self._ser.in_waiting or 1)
        if not rx_data:
            return
        _log.debug('Data mode received %d bytes at %0.1f', len(rx_data), now)
        for b in rx_data:
            self.data_mode_data.append(b)
            if not self._data_mode_exit:
                self._last_data_mode_rx_time = now
                continue
            expected = self._data_mode_exit.encode()
            expected_byte = expected[self._data_mode_exit_match_idx]
            if b == expected_byte:
                if self._data_mode_exit_match_idx == 0:
                    self._data_mode_exit_start = now
                    _log.debug('Started exit sequence matching at %0.2f', now)
                self._data_mode_exit_match_idx += 1
                if self._data_mode_exit_match_idx == len(expected):
                    idle = (self._data_mode_exit_start - self._last_data_mode_rx_time)
                    if idle >= self._data_mode_exit_delay:
                        _log.debug('Exit sequence detected after %0.2fs', idle)
                        # Remove exit sequence from received data
                        self.data_mode_data = self.data_mode_data[:-len(expected)]
                        self._reset_data_mode_state()
                        return
                    else:
                        _log.debug('Exit sequence ignored - idle only %0.2fs',
                                   idle)
                        self._data_mode_exit_match_idx = 0
            else:
                if self._data_mode_exit_match_idx > 0:
                    _log.debug('Exit sequence broken - reset match')
                self._data_mode_exit_match_idx = 0
                self._last_data_mode_rx_time = now
        
        if self._data_mode_exit == '<auto>' and not self._ser.in_waiting:
            _log.debug('Auto exit from data mode')
            self._reset_data_mode_state()
        # time.sleep(0.1)
    
    def _handle_data_mode_tx(self,
                             data: bytes,
                             delay: Union[float, int] = 0,
                             auto_exit: bool = True,
                             is_intermediate: bool = False):
        """Sends predefined data to the DTE in data mode"""
        tag = 'intermediate' if is_intermediate else 'post-result'
        if delay:
            _log.debug('Delaying %s data %0.1f seconds', tag, delay)
        self.data_mode = True
        time.sleep(delay)
        _log.debug('Sending %d bytes %s data', len(data), tag)
        self._ser.write(data)
        self._ser.flush()
        time.sleep(0.1)   # allow DTE to receive the data before exiting
        if auto_exit is True:
            self.data_mode = False
        
    def set_binary_handler(self,
                           handler: Callable[[serial.Serial, Literal['recv', 'send'], Optional[bytes]], None],
                           direction: Literal['recv', 'send'] = 'recv',
                           data: Optional[bytes] = None,
                           **kwargs):
        """Assign a handler to run in data mode."""
        self._binary_handler = lambda ser: handler(ser, direction, data, **kwargs)
    
    def _handle_command_mode(self):
        """"""
        b = self._ser.read()
        try:
            c = b.decode() if b else self.terminator   # set terminator for data_mode exit case
            if c not in string.printable:
                raise UnprintableException
            if c != self.terminator:
                self._request += c
                return
            if not self._request:
                return
            
            request = self._request
            responses = self.commands
            _log.debug('Processing command: %s', dprint(request))
            echo = self._request + self.terminator if self.echo else ''
            intermediate_response = ''
            enter_data_mode: Union[bool, str, None] = None
            use_xmodem: bool = False
            data_mode_send: Union[bytes, None] = None
            data_mode_tx_exit: bool = False
            response = ''
            response_delay = 0
            data_delay = 0
            
            if request.upper() == 'AT':
                response = VRES_OK if self.verbose else RES_OK
            
            elif request.upper().startswith('ATE'):
                self.echo = request.endswith('1')
                response = VRES_OK if self.verbose else RES_OK
            
            elif request.upper().startswith('ATV'):
                self.verbose = request.endswith('1')
                _log.debug('Verbose %sabled', 'en' if self.verbose else 'dis')
                response = VRES_OK if self.verbose else RES_OK
            
            elif (any(request.startswith(c) for c in responses)):
                _log.debug('Processing custom response')
                matched_key = max((k for k in responses if request.startswith(k)),
                                  key=len, default=None)
                if not matched_key:
                    raise ValueError('Unable to find %s', request)
                res_meta = responses.get(matched_key)
                if res_meta.get('hasEcho') is True:
                    echo = ''
                intermediate_response = res_meta.get('intermediateResponse', '')
                data_mode_send = res_meta.get('dataReply', '').encode()
                use_xmodem = res_meta.get('xmodem', False)
                data_delay = res_meta.get('dataDelay', 0)
                response = res_meta.get('response', '')
                response_delay = res_meta.get('delay', 0)
                
                enter_data_mode = res_meta.get('dataModeEntry')
                if enter_data_mode:
                    _log.debug('Command triggers data mode')
                    self._data_mode_exit = res_meta.get('dataModeExitSequence')
                    self._data_mode_exit_res = res_meta.get('dataModeExitResponse')
                    self._data_mode_exit_delay = res_meta.get('dataExitDelay', 0)
                    if (not self._data_mode_exit or
                        not self._data_mode_exit_res):
                        raise ValueError('Data mode exit not defined'
                                        f' for {res_meta}')
                    self.data_mode_data = bytearray()
                    self._last_data_mode_rx_time = 0
                    if data_mode_send:
                        data_mode_tx_exit = self._data_mode_exit == '<auto>'
                    elif intermediate_response:
                        self.data_mode = True
                
                if not response and not self._data_mode_exit_res:
                    raise ValueError('No command response defined')
                    
            else:
                _log.error('Unsupported command: %s', request)
                response = VRES_ERR if self.verbose else RES_ERR

            if echo:
                _log.debug('Sending echo: %s', dprint(echo))
                self._ser.write(echo.encode())
                self._ser.flush()
            
            if use_xmodem:
                direction = 'send' if data_mode_send else 'recv'
                _log.debug('Preparing XMODEM to %s data', direction)
                self.set_binary_handler(xmodem_bytes_handler,
                                        direction,
                                        data_mode_send,
                                        # getc_timeout=5,
                                        )
                self.data_mode = True

            if intermediate_response:
                self.intermediate_pause = res_meta.get('intermediatePause', False)
                paused = self.intermediate_pause
                _log.info('Sending intermediate response to %s: %s',
                            dprint(request),
                            dprint(intermediate_response))
                self._ser.write(intermediate_response.encode())
                self._ser.flush()
                time.sleep(0.1)
                if paused:
                    _log.warning('Paused waiting to reset intermediate_pause')
                while self.intermediate_pause:
                    time.sleep(0.5)
                if paused:
                    _log.debug('Intermediate pause completed')

            if use_xmodem:
                result = self._binary_handler(self._ser)
                if isinstance(result, bytes):
                    self.data_mode_data = result.rstrip(b'\x1A')
                    _log.debug('Received: %r', self.data_mode_data)
                else:
                    _log.debug('Result: %s', result)
                self._reset_data_mode_state()
            
            if response and not self.data_mode:
                if response_delay:
                    _log.debug('Delaying response %0.1f seconds', response_delay)
                time.sleep(response_delay)
                
                if data_mode_send and intermediate_response and not use_xmodem:
                    self._handle_data_mode_tx(data_mode_send,
                                              delay=data_delay,
                                              auto_exit=data_mode_tx_exit,
                                              is_intermediate=True,)
                    data_mode_send = None   # clear to avoid resending post-result
                    time.sleep(0.1)
                
                if not self.verbose:
                    # Remove verbose headers/trailers
                    pattern = r'\r\n.*?\r\n'
                    lines: list[str] = re.findall(pattern, response, re.DOTALL)
                    if lines:
                        for i, line in enumerate(lines):
                            lines[i] = line.replace('\r\n', '', 1)
                            if i == len(lines) - 1:
                                lines[i] = RES_OK if 'OK' in line else RES_ERR
                        response = ''.join(lines)
                
                to_write = response.encode()
                
                if 'BAD_BYTE' in request:
                    # since we can't store escaped non-printable in commands.json
                    bad_byte = 0xFF
                    to_write = bytearray(to_write)
                    position = request[-2]
                    if position == 'B':
                        bad_byte_offset = 0
                    elif position == 'M':
                        bad_byte_offset = int(len(to_write) / 2)
                    else:
                        bad_byte_offset = len(to_write) - 1
                    to_write.insert(bad_byte_offset, bad_byte)
                
                _log.debug('Sending final response to %s: %s',
                           dprint(request or 'data mode exit'),
                           dprint(to_write.decode(errors='backslashreplace')))
                self._ser.write(to_write)
                self._ser.flush()
                time.sleep(0.1)
            
                if isinstance(enter_data_mode, str):
                    _log.debug('Sending data mode entry URC')
                    self._ser.write(enter_data_mode.encode())
                    self._ser.flush()
                    self.data_mode = True
                    time.sleep(0.1)
                
            if data_mode_send and not use_xmodem:
                self._handle_data_mode_tx(data_mode_send,
                                        delay=data_delay,
                                        auto_exit=data_mode_tx_exit)

            # clear for next request
            self._request = ''
            
        except (UnicodeDecodeError, UnprintableException):
            _log.error('Bad byte received [%d] - clearing buffer\n', b[0])
            self._request = ''
        
    def _run(self):
        while self._running:
            if not self._ser or not self._ser.is_open:
                self.stop()
                _log.error('Serial port closed')
                return
            if self.data_mode:
                self._handle_data_mode_rx()
            elif self._data_mode_exit_res:
                _log.debug('Sending final response after data mode exit: %s',
                           dprint(self._data_mode_exit_res))
                self._ser.write(self._data_mode_exit_res.encode())
                self._data_mode_exit_res = None
            else:
                self._handle_command_mode()
    
    def inject_urc(self, urc: str, v0_header: str = '\r\n'):
        """Inject an unsolicited response code."""
        if not isinstance(urc, str) or not urc:
            _log.error('Invalid URC')
            return
        if self.verbose:
            urc = f'\r\n{urc}\r\n'
        else:
            urc = f'{v0_header}{urc}\r\n'
        _log.debug('Sending URC: %s', dprint(urc))
        self._ser.write(urc.encode())
        self._ser.flush()
    
    def multi_urc(self, urcs: 'list[str]', v0_header: str = '\r\n'):
        """Inject multiple unsolicited outputs."""
        if self.verbose:
            chained = '\r\n'.join(f'\r\n{urc}' for urc in urcs)
        else:
            chained = '\r\n'.join(f'{v0_header}{urc}' for urc in urcs)
        chained += '\r\n'
        _log.debug('Sending chained URCS: %s', dprint(chained))
        self._ser.write(chained.encode())
        self._ser.flush()
    
    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join()
            self._thread = None
        if self._ser and self._ser.is_open:
            self._ser.close()
            self._ser = None
            

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
    from .serialbridge import SerialBridge
    
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
