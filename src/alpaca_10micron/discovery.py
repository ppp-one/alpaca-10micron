"""Alpaca discovery: answer UDP broadcasts on port 32227 with the HTTP port of this server."""

import asyncio
import json
import logging
import socket

logger = logging.getLogger(__name__)

DISCOVERY_MESSAGE = b"alpacadiscovery1"


class DiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self, alpaca_port: int):
        self.reply = json.dumps({"AlpacaPort": alpaca_port}).encode()
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr):
        if data.startswith(DISCOVERY_MESSAGE) and self.transport is not None:
            self.transport.sendto(self.reply, addr)


async def start_discovery(alpaca_port: int, discovery_port: int) -> asyncio.DatagramTransport:
    """Listen for discovery requests (IPv4). Other Alpaca servers may share the port."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", discovery_port))
        sock.setblocking(False)
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: DiscoveryProtocol(alpaca_port), sock=sock
        )
    except BaseException:
        sock.close()
        raise
    logger.info("Alpaca discovery listening on UDP port %d", discovery_port)
    return transport
