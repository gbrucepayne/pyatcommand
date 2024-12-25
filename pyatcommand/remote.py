"""A remote serial server for connecting to a modem over a network.

Runs on a remote computer e.g. Raspberry Pi physically connected to a
modem that interfaces using AT commands.
"""

import logging
import os
import serial
import socket
import threading


SERIAL_PORT = os.getenv('SERIAL_PORT', '/dev/ttyUSB0')
BAUDRATE = int(os.getenv('BAUDRATE', '9600'))
HOST = os.getenv('SERIAL_HOST', '0.0.0.0')
PORT = int(os.getenv('SERIAL_TCP_PORT', '12345'))

_log = logging.getLogger(__name__)


class SerialSocketServer:
    def __init__(self,
                 serial_port: str = SERIAL_PORT,
                 baud_rate: int = BAUDRATE,
                 host: str = HOST,
                 port: int = PORT):
        self.serial_port = serial_port
        self.baud_rate = baud_rate
        self.host = host
        self.port = port
        self.serial = None
        self.server_socket = None
        self.running = False

    def start(self):
        """Starts the serial socket server."""
        try:
            # Initialize the serial connection
            self.serial = serial.Serial(self.serial_port, self.baud_rate, timeout=0)
            _log.info('Serial connection opened on %s at %d baud',
                      self.serial_port, self.baud_rate)

            # Initialize the socket server
            self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server_socket.bind((self.host, self.port))
            self.server_socket.listen(1)
            _log.info('Socket server listening on %s:%d', self.host, self.port)

            self.running = True
            while self.running:
                _log.info('Waiting for a client to connect...')
                client_socket, client_address = self.server_socket.accept()
                _log.info('Client connected from %s', client_address)

                # Start threads for bidirectional communication
                threading.Thread(target=self.handle_socket_to_serial,
                                 args=(client_socket,), daemon=True).start()
                threading.Thread(target=self.handle_serial_to_socket,
                                 args=(client_socket,), daemon=True).start()

        except Exception as e:
            print(f"Error: {e}")
        finally:
            self.stop()

    def stop(self):
        """Stops the serial socket server and cleans up resources."""
        self.running = False
        if self.serial and self.serial.is_open:
            self.serial.close()
        if self.server_socket:
            self.server_socket.close()
        _log.info('Server stopped')

    def handle_socket_to_serial(self, client_socket: socket.socket):
        """Handles data from the socket and sends it to the serial port."""
        try:
            while self.running:
                data = client_socket.recv(1024)
                if not data:
                    break
                _log.debug('Socket->Serial: %s', data.decode(errors='ignore'))
                self.serial.write(data)
        except Exception as e:
            _log.error('Socket to serial error: %s', e)
        finally:
            client_socket.close()

    def handle_serial_to_socket(self, client_socket: socket.socket):
        """Handles data from the serial port and sends it to the socket."""
        try:
            while self.running:
                data = self.serial.read(self.serial.in_waiting or 1)
                if data:
                    _log.debug('Serial->Socket: %s', data.decode(errors='ignore'))
                    client_socket.sendall(data)
        except Exception as e:
            _log.error('Serial to socket error: %s', e)
        finally:
            client_socket.close()


if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG)
    server = SerialSocketServer()
    try:
        server.start()
    except KeyboardInterrupt:
        _log.info('Stopping...')
        server.stop()
