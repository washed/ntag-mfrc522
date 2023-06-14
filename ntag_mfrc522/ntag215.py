import io
from time import sleep
from typing import Iterator, Optional

import more_itertools
import ndef

from ntag_mfrc522.mfrc522 import MFRC522


def get_ndef_partition(data: bytes) -> bytes:
    if data[0] != 0x03:
        raise ValueError

    return data[2 : 2 + data[1]]


def prepend_ndef_partition_header(data: bytes) -> bytes:
    return bytearray([0x03, len(data)]) + data


def to_hex_string(numbers: bytes):
    return ":".join(f"{i:02X}" for i in numbers)


class NTag215:
    PAGE_SIZE_BYTES = 4
    BLOCK_SIZE_BYTES = 16
    BLOCK_PAGE_OFFSET = BLOCK_SIZE_BYTES // PAGE_SIZE_BYTES
    PAGE_COUNT = 135
    FULL_BLOCK_COUNT = PAGE_COUNT * PAGE_SIZE_BYTES // BLOCK_SIZE_BYTES
    PARTIAL_BLOCKS_PAGE_COUNT = int(
        (PAGE_COUNT * PAGE_SIZE_BYTES / BLOCK_SIZE_BYTES)
        % 1
        * BLOCK_SIZE_BYTES
        // PAGE_SIZE_BYTES
    )

    # memory region offsets (ends are inclusive)
    UID_START = 0
    UID_END = 8

    INTERNAL_START = 9
    INTERNAL_END = 9

    LOCK_BYTES_START = 10
    LOCK_BYTES_END = 11

    CAPABILITY_CONTAINER_START = 12
    CAPABILITY_CONTAINER_END = 15

    USER_MEMORY_START = 16
    USER_MEMORY_END = 515

    DYNAMIC_LOCK_BYTES_START = 516
    DYNAMIC_LOCK_BYTES_END = 518

    RFUI_0_START = 519
    RFUI_0_END = 519

    CFG_0_START = 520
    CFG_0_END = 523

    CFG_1_START = 524
    CFG_1_END = 527

    PWD_START = 528
    PWD_END = 531

    PACK_START = 532
    PACK_END = 533

    RFUI_1_START = 534
    RFUI_1_END = 535

    _memory: bytearray = bytearray()

    def __init__(self):
        super().__init__()
        self.mfrc522 = MFRC522()

        self._uid: Optional[bytes] = None

    def _read_no_block(self):
        _tag_type = self.mfrc522.request_tag()
        self._uid = self.mfrc522.select_tag()

        print("reading from tag with uid: ", to_hex_string(self._uid))

        for block_num in range(self.FULL_BLOCK_COUNT):
            page_addr = block_num * self.BLOCK_PAGE_OFFSET
            block = self.mfrc522.read_block(page_addr)
            if block:
                self._memory.extend(block)

        if self.PARTIAL_BLOCKS_PAGE_COUNT != 0:
            block = self.mfrc522.read_block(
                self.FULL_BLOCK_COUNT * self.BLOCK_PAGE_OFFSET
            )
            if block:
                self._memory.extend(
                    block[0 : self.PARTIAL_BLOCKS_PAGE_COUNT * self.PAGE_SIZE_BYTES]
                )

    def _write_no_block(self, text: str):
        _tag_type = self.mfrc522.request_tag()
        uid = self.mfrc522.select_tag()

        record = ndef.TextRecord(text, "en")

        stream = io.BytesIO()
        encoder = ndef.message_encoder([record], stream)
        for _ in encoder:
            pass

        octets = stream.getvalue()
        octets = prepend_ndef_partition_header(octets)
        print("octets: ", octets)
        for i, chunk in enumerate(more_itertools.chunked(octets, 4)):
            if len(chunk) < 4:
                chunk += [0] * (4 - len(chunk))
            print(i, chunk)
            self.mfrc522.write_block(4 + i, chunk)

        return uid, octets

    def read(self):
        while True:
            try:
                self._read_no_block()
                break
            except:
                sleep(0.2)

    def write(self, text: str):
        id, text_in = self._write_no_block(text)
        while not id:
            id, text_in = self._write_no_block(text)
        return id, text_in

    def get_ndef_records(self) -> Iterator[ndef.record.GlobalRecord]:
        if len(self._memory) == 0:
            raise RuntimeError(
                "Memory seems to be empty, be sure to run `read()` first!"
            )

        ndef_partition = get_ndef_partition(self.user_memory_raw)
        return ndef.message_decoder(ndef_partition)

    def print_memory(self) -> None:
        for chunk in more_itertools.chunked(self._memory, self.BLOCK_SIZE_BYTES):
            print(to_hex_string(bytes(chunk)))

    @property
    def uid_raw(self) -> bytes:
        return self._memory[self.UID_START : self.UID_END + 1]

    @property
    def user_memory_raw(self) -> bytes:
        return self._memory[self.USER_MEMORY_START : self.USER_MEMORY_END + 1]

    @property
    def uid(self) -> Optional[bytes]:
        return self._uid
