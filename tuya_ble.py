"""Minimal Tuya Bluetooth LE client: just enough to write datapoints to a bulb.

The packet format follows the MIT-licensed ha_tuya_ble project
(https://github.com/PlusPlus-ua/ha_tuya_ble). Needs the device's uuid, device id
and local key, the same credentials the Wi-Fi path uses.
"""
import asyncio
import hashlib
import secrets
import struct
import time

from bleak import BleakClient, BleakScanner
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

SERVICE_UUIDS = (
    "0000a201-0000-1000-8000-00805f9b34fb",
    "0000fd50-0000-1000-8000-00805f9b34fb",
)
CHAR_NOTIFY = "00002b10-0000-1000-8000-00805f9b34fb"
CHAR_WRITE = "00002b11-0000-1000-8000-00805f9b34fb"
MANUFACTURER_ID = 0x07D0
GATT_MTU = 20        # chunk size every Tuya device accepts; used for the handshake
MAX_CHUNK = 200      # after the handshake a whole command goes out in one write if the link allows

DEVICE_INFO, PAIR, WRITE_DPS = 0x0000, 0x0001, 0x0002
DP_REPORT, TIME_REQUEST = 0x8001, 0x8011
DP_BOOL, DP_STRING, DP_ENUM = 1, 3, 4


def dp(dp_id, dp_type, value):
    """Encode one datapoint for write_dps(). `value` is bytes."""
    return struct.pack(">BBB", dp_id, dp_type, len(value)) + value


def _aes(key, iv, data, encrypt):
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    op = cipher.encryptor() if encrypt else cipher.decryptor()
    return op.update(data) + op.finalize()


def _crc16(data):
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def _pack_int(value):
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | 0x80 if value else byte)
        if not value:
            return out


def _unpack_int(data, pos):
    result = shift = 0
    while True:
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return result, pos


def decode_advertisement(adv):
    """Return {"uuid", "protocol", "bound"} for a Tuya advertisement, else {}."""
    product = None
    for uuid in SERVICE_UUIDS:
        data = adv.service_data.get(uuid)
        # First byte: 0 = unbound (plain product id), 1 = bound (encrypted product id).
        if data and len(data) > 1 and data[0] in (0, 1):
            product = bytes(data[1:])
    mfr = adv.manufacturer_data.get(MANUFACTURER_ID)
    if not product or not mfr or len(mfr) <= 6 or len(mfr[6:]) % 16:
        return {}
    key = hashlib.md5(product).digest()
    uuid = _aes(key, key, bytes(mfr[6:]), False).decode("utf-8", "replace")
    return {"uuid": uuid, "protocol": mfr[1], "bound": bool(mfr[0] & 0x80)}


class TuyaBle:
    def __init__(self, client, uuid, device_id, local_key, protocol):
        self.client = client
        self.uuid, self.device_id = uuid, device_id
        self.key_seed = local_key[:6].encode()
        self.login_key = hashlib.md5(self.key_seed).digest()
        self.session_key = None
        self.protocol = protocol
        self.chunk, self.chunk_confirmed = GATT_MTU, False
        self.seq = 0
        self.buffer, self.expected_len, self.expected_num = bytearray(), 0, 0
        self.waiters = {}
        self.write_lock = asyncio.Lock()

    @property
    def connected(self):
        return self.client.is_connected

    async def _send(self, code, data=b"", response_to=0, wait=True, timeout=8):
        self.seq += 1
        seq = self.seq
        key, flag = (self.login_key, b"\x04") if code == DEVICE_INFO else (self.session_key, b"\x05")
        raw = bytearray(struct.pack(">IIHH", seq, response_to, code, len(data)) + data)
        raw += struct.pack(">H", _crc16(raw))
        raw += b"\x00" * (-len(raw) % 16)
        iv = secrets.token_bytes(16)
        encrypted = flag + iv + _aes(key, iv, bytes(raw), True)

        future = None
        if wait:
            future = asyncio.get_running_loop().create_future()
            self.waiters[seq] = future
        # One logical packet may go out as several GATT writes; never interleave two.
        async with self.write_lock:
            pos = num = 0
            while pos < len(encrypted):
                packet = bytearray(_pack_int(num))
                if num == 0:
                    packet += _pack_int(len(encrypted))
                    packet.append(self.protocol << 4)
                part = encrypted[pos:pos + self.chunk - len(packet)]
                packet += part
                pos += len(part)
                num += 1
                await self.client.write_gatt_char(CHAR_WRITE, bytes(packet), response=False)
        if future:
            try:
                return await asyncio.wait_for(future, timeout)
            except asyncio.TimeoutError:
                self.waiters.pop(seq, None)
                return None

    def _on_notify(self, _, data):
        num, pos = _unpack_int(data, 0)
        if num == 0:
            self.buffer, self.expected_num = bytearray(), 0
            self.expected_len, pos = _unpack_int(data, pos)
            pos += 1
        if num != self.expected_num:
            return
        self.buffer += data[pos:]
        self.expected_num += 1
        if len(self.buffer) >= self.expected_len:
            packet, self.buffer, self.expected_num = bytes(self.buffer[:self.expected_len]), bytearray(), 0
            self._on_packet(packet)

    def _on_packet(self, packet):
        key = {4: self.login_key, 5: self.session_key}.get(packet[0])
        if key is None or len(packet) < 33:
            return
        raw = _aes(key, packet[1:17], packet[17:], False)
        seq, response_to, code, length = struct.unpack(">IIHH", raw[:12])
        data = raw[12:12 + length]
        if len(raw) < 14 + length:
            return
        if struct.unpack(">H", raw[12 + length:14 + length]) != (_crc16(raw[:12 + length]),):
            return
        if code == DEVICE_INFO and len(data) >= 12:
            self.protocol = data[2]
            self.session_key = hashlib.md5(self.key_seed + data[6:12]).digest()
        future = self.waiters.pop(response_to, None)
        if future and not future.done():
            future.set_result(data)
        elif code == DP_REPORT:
            self._reply(code, b"", seq)
        elif code == TIME_REQUEST:
            now = str(int(time.time() * 1000)).encode() + struct.pack(">h", -int(time.timezone / 36))
            self._reply(code, now, seq)

    def _reply(self, code, data, seq):
        task = asyncio.ensure_future(self._send(code, data, response_to=seq, wait=False))
        task.add_done_callback(lambda t: t.cancelled() or t.exception())

    async def _handshake(self):
        await self.client.start_notify(CHAR_NOTIFY, self._on_notify)
        await self._send(DEVICE_INFO)
        if not self.session_key:
            raise RuntimeError("the bulb did not answer the Bluetooth handshake")
        request = self.uuid.encode() + self.key_seed + self.device_id.encode()
        result = await self._send(PAIR, request.ljust(44, b"\x00"))
        # 0 = paired, 2 = already paired with these credentials
        if not result or result[0] not in (0, 2):
            raise RuntimeError("the bulb rejected the local key (was it re-paired?)")
        self.chunk = max(GATT_MTU, min(self.client.mtu_size - 3, MAX_CHUNK))

    async def write_dps(self, payload, timeout=8):
        """Write datapoints built with dp(). Returns True once the bulb acknowledges them."""
        result = await self._send(WRITE_DPS, payload, timeout=timeout)
        if result is not None:
            self.chunk_confirmed = True
        elif not self.chunk_confirmed:
            self.chunk = GATT_MTU  # this bulb may not take large writes
        return bool(result is not None and result[:1] == b"\x00")

    async def disconnect(self):
        await self.client.disconnect()


async def connect(uuid, device_id, local_key, scan_timeout=15):
    """Find the bulb by uuid, connect and authenticate. Raises RuntimeError on failure."""
    found = {}

    def is_target(_, adv):
        info = decode_advertisement(adv)
        if info.get("uuid") == uuid:
            found.update(info)
            return True
        return False

    device = await BleakScanner.find_device_by_filter(is_target, timeout=scan_timeout)
    if device is None:
        raise RuntimeError("bulb not found over Bluetooth")
    client = BleakClient(device, timeout=20)
    await client.connect()
    bulb = TuyaBle(client, uuid, device_id, local_key, found["protocol"])
    try:
        await bulb._handshake()
    except BaseException:
        await client.disconnect()
        raise
    return bulb
