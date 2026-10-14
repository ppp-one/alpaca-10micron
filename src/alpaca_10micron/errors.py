"""ASCOM error codes and the exceptions that map to Alpaca error responses."""

# ASCOM error numbers (see the ASCOM Alpaca API, "Error handling").
NOT_IMPLEMENTED = 0x400
INVALID_VALUE = 0x401
VALUE_NOT_SET = 0x402
NOT_CONNECTED = 0x407
INVALID_WHILE_PARKED = 0x408
INVALID_OPERATION = 0x40B
ACTION_NOT_IMPLEMENTED = 0x40C
DRIVER_ERROR = 0x500  # 0x500-0xFFF are driver-specific errors


class AlpacaError(Exception):
    """An ASCOM error. Returned as HTTP 200 with ErrorNumber and ErrorMessage."""

    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


class BadRequest(Exception):
    """The request cannot be understood (missing or malformed parameter, unknown device).

    Returned as HTTP 400 with a plain-text message, as the Alpaca API requires.
    """


def not_implemented(member: str) -> AlpacaError:
    return AlpacaError(NOT_IMPLEMENTED, f"{member} is not implemented by this driver")


def not_connected() -> AlpacaError:
    return AlpacaError(NOT_CONNECTED, "The telescope is not connected")
