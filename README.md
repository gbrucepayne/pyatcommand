# ITU-T V.250 AT Command Parser for Python

This was developed due to seeming lack of a generalized AT command processing
library in the PyPI ecosystem. Several other implementations exist for more
specific purposes but miss certain functions such as non-verbose (`V0`) mode,
echo on/off (`E1`), or unsolicited result codes (URC) and related complexities
involving solicited/unsolicited race conditions.

Data Terminating Equipment **DTE** refers to the device talking to the modem
i.e. originating commands and would implement the **`AtClient`**.

Data Communications Equipment **DCE** refers to the modem responding to commands
or emitting unsolicited information and would implement the **`AtServer`**.

Use of Verbose mode (`ATV1`) is generally recommended to disambiguate responses, errors and URCs.

Use of Echo (`ATE1`) is generally recommended to disambiguate responses from
URCs.

Although the standard allows changing the `<cr>` and `<lf>` characters, this
is rarely practical in modern systems, and is generally discouraged.

## Client

The client functionality is used to talk to a modem (or anything similar that
supports ITU-T V.250 style AT commands).

Allows for processing of command/response or receipt of unsolicited result code
(URC) emitted by the modem. Also includes an optional 16-cit CRC validation
supported by some modems.

Output from the modem 

### Connection and Initialization

The `AtClient` instance can be configured with settings using initialization
parameters or when using the `connect()` method. Typical parameters include:
* The serial `port` name
* `baudrate` (default `9600`)
* `echo` (default `True`)
* `verbose` (default `True`)
* `autobaud` (default `True`) will cycle through supported baudrates trying to
attach using a validated `AT` command

Once the basic AT command returns a valid response, the background listening
thread is started and the device gets initialized to a known AT configuration
for `echo` and `verbose`.

### Command/Response

This is the main mode of intended use. The logic flow is as follows:

An AT commmand, with optional timeout, is submitted by calling `send_command()`:
* Uses a `threading.Lock` to ensure thread safety;
* Clears the last error code;
* Clears the response buffer;
* (Optional) calculates and applies CRC to the command;
* Applies the command line termination character (default `\r`);
* Sends the command on serial and waits for all data to be sent;
* Sets the pending command state;
* Waits for a response or timeout;
* If no timeout is specified, the default is 0.3 seconds (`AT_TIMEOUT`).
    * A default timeout can be set with `AT_TIMEOUT` environment variable
    or by using the `command_timeout` init parameter or property.
* A timed-out response returns `None`;
* A valid response returns an `AtResponse` object with properties:
    * `ok` (`bool`) Indicating a successful result
    * `info` (`str`) If the response included text. Multiple lines are
    separated by newline ('\n').
    Any CME/CMS ERROR results will be placed in `info` when `ok` is `False`
    * `crc_ok` (`bool` or `None`) If CRC feature is supported, indicates if
    the response had a valid CRC.

### Unsolicited Response Codes (URC)

Once the connection is established and the AT command parameters configured,
a background thread is started to listen for any incoming serial data.
Check for unsolicited data using `get_urc()` which returns the next queued URC.

URCs are assumed to always be prefixed and suffixed by `<cr><lf>` regardless
of the Verbose setting. URCs are also assumed to be a single line
i.e. no `<cr><lf>` in the middle of a URC.

Race conditions can occur when a URC arrives just as a command is being sent
resulting in the possibility of the URC(s) being prepended to the response.
To avoid such conditions you can ensure Echo is enabled so that responses can
be distinguished from unsolicited by the presence of an echo.

### CRC Error Detection

An optional CRC-16-CCITT feature is supported that appends a 16-bit error
detection mechanism when supported by the device. A `crc_sep` separator
can be defined (default `*`) that is followed by a 4-character hexadecimal
value. The CRC is applied before the command terminator
(e.g. `AT*3983\r`) or after the result code (e.g. `\r\nOK\r\n*86C5\r\n`).

The command string must be specified in the property `crc_enable`
e.g. `client.crc_enable = 'AT%CRC=1'`.
The corresponding disable string can either be derived as the numeric zero of
the enabler, or manually set using the `crc_disable` property.

### Lecacy Client

The original version of this library operated as follows and is supported.
This approach can also be used to retrieve *full raw* responses including all
formatting characters.

#### Legacy Command/Response support

1. AT commmand, with optional timeout, is submitted by a function call
`send_at_command()` which:
    * If a prior command is pending (TBC thread-safe) waits for a `ready`
    Event to be set by the completion of the prior command;
    * Clears the last error code;
    * Clears the receive buffer;
    * (Optional) calculates and applies CRC to the command;
    * Applies the command line termination character (default `\r`);
    * Sends the command on serial and waits for all data to be sent;
    * Sets the pending command state;
    * Calls an internal response parsing function and returns an `AtErrorCode`
    code, with 0 (`OK`) indicating success;
    * If no timeout is specified, the default is 1 second
    (`AT_TIMEOUT`).

2. Response parsing:
    * Transitions through states `ECHO`, `RESPONSE`, (*optional*) `CRC`
    to either `OK` or `ERROR`;
    * If timeout is exceeded, parsing stops and indicates
    `AtErrorCode.ERR_TIMEOUT`;
    * (Optional) validation of checksum, failure indicates
    `AtErrorCode.ERR_CMD_CRC`;
    * Other modem error codes received will be indicated transparently;
    * Successful parsing will place the response into a buffer for retrieval;
    * Sets the last error code or `OK` (0) if successful;
    * Clears the pending command state, and sets the `ready` Event.

3. Retrieval of successful response is done using `get_response()`
with an optional `prefix` to remove.
All other leading/trailing whitespace is removed, and multi-line responses are
separated by a single line feed (`\n`). Retrieval clears the *get* buffer.

    >[!NOTE]
    >Optional parameter `clean = False` will return the full raw response with
    all formatting characters.

4. A function `last_error_code()` is intended to be defined for modems
that support this concept (e.g. query `S80?` on Orbcomm satellite modem).

#### Legacy Unsolicited Result Codes (URC)

Some modems emit unsolicited codes. In these cases it is recommended that the
application checks/retrieves any URC(s) prior to submitting any AT command.

`check_urc()` simply checks if any serial data is waiting when no AT command is
pending, and if present parses until both command line termination and response
formatting character have been received or timeout (default 1 second
`AT_URC_TIMEOUT`).
URC data is placed in the *get* buffer and retrieved in the same way as a
commmand response.

### CRC support

Currently a CCITT-16-CRC option is supported for commands and responses. The
enable/disable command may be configured using `+CRC=<1|0>`.
(`%CRC=<1|0>` also works)

## Server (Work in Progress)

The server concept is to act as a modem/proxy replying to a microcontroller.

You register custom commands using `add_command()` with a data structure that
includes the command `name` and optional callback functions for `read`, `run`,
`test` and `write` operations.

`Verbose` and `Echo` features are supported using the standard `V` and `E`
commands defined in the V.25 spec.

`CRC` is an optional extended command to support 16-bit checksum validation of
requests and responses that can be useful in noisy environments.

### Feature considerations

* Repeating a command line using `A/` or `a/` is not supported;
* No special consideration is given for numeric or string constants, those are
left to custom handling functions;
* Concatenation of basic commands deviates from the standard and expects a
semicolon separator;

### Acknowledgements

The server idea is based somewhat on the
[ATCommands](https://github.com/yourapiexpert/ATCommands)
library which had some shortcomings for my cases including GPL, and
[cAT](https://github.com/marcinbor85/cAT) but reframed for C++.
Many thanks to those developers for some great ideas!