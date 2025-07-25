"""Serial bridge utility for simulating DCE or DTE connections to unit tests.
"""
import atexit
import logging
import os
import platform
import shutil
import signal
import subprocess
import threading
import time

from serial.tools.list_ports import comports


BAUDRATE = int(os.getenv('BAUDRATE', '9600'))
DCE = os.getenv('DCE', './simdce')
DTE = os.getenv('DTE', './simdte')
SERIAL_BRIDGE_DELAY_S = 1

_log = logging.getLogger(__name__)


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
        if shutil.which('socat') is None:
            _log.error('socat is not installed or not in PATH')
            raise FileNotFoundError('socat command not found')
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
        try:
            self._process = subprocess.Popen(cmd,
                                             stdout=subprocess.PIPE,
                                             stderr=subprocess.PIPE,
                                             shell=True,
                                             preexec_fn=os.setsid)
            
            def _monitor_stderr():
                for line in iter(self._process.stderr.readline, b''):
                    _log.debug('socat: %s', line.decode(errors='ignore').strip())
            
            threading.Thread(target=_monitor_stderr,
                             name='serial_bridge',
                             daemon=True).start()
            # self._stdout, self._stderr = self._process.communicate()
        except Exception as exc:
            _log.error('Failed to start socat: %s', exc)
            raise
        
    def start(self):
        """Start the simulated serial interface."""
        if platform.system() == 'Windows':
            if not all(x in comports for x in [self.dce, self.dte]):
                raise IOError('Invalid COM ports')
            _log.info('Using preconfigured com0com ports: %s <--> %s',
                      self.dce, self.dte)
            time.sleep(SERIAL_BRIDGE_DELAY_S)
        else:
            try:
                self._thread = threading.Thread(target=self._socat,
                                                name='serial_bridge',
                                                daemon=True)
                self._thread.start()
                time.sleep(SERIAL_BRIDGE_DELAY_S)
            except Exception as exc:
                _log.error('Serial bridge startup failed: %s', exc)
    
    def stop(self):
        """Stop the bridge (if running on socat)."""
        if isinstance(self._thread, threading.Thread) and self._thread.is_alive():
            try:
                # self._process.kill()
                os.killpg(os.getpgid(self._process.pid), signal.SIGTERM)
                self._process.wait(timeout=2)
                _log.debug('Serial bridge socat stopped cleanly')
            except Exception as err:
                _log.error('Serial bridge stop failed: %s', err)
