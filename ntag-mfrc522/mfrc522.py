import logging
from typing import List, Tuple

import constants

import gpiod
import spidev

"""
TODO

* use interrupt pin
* don't use magic numbers for register config
* logging
* use bytearray/bytes instead of List[int]
"""


class MFRC522:
    def __init__(
        self,
        bus: int = 0,
        device: int = 0,
        spd: int = 1000000,
        pin_rst: str = "GPIO25",
        log_level: str = "WARNING",
    ) -> None:
        super().__init__()
        self.spi = spidev.SpiDev()
        self.spi.open(bus, device)
        self.spi.max_speed_hz = spd

        self.logger = logging.getLogger("mfrc522Logger")
        self.logger.addHandler(logging.StreamHandler())
        level = logging.getLevelName(log_level)
        self.logger.setLevel(level)

        line_ref = gpiod.find_line(pin_rst)
        chip = line_ref.get_chip()
        self._reset_pin_line = chip.get_line(line_ref.offset)

        config = gpiod.line_request()
        config.request_type = gpiod.line_request.DIRECTION_OUTPUT
        self._reset_pin_line.request(config=config, default_val=1)

        # TODO: do irq pin config here

        self._init()

    def _init(self) -> None:
        self._reset()

        self._write(constants.TModeReg, 0x8D)
        self._write(constants.TPrescalerReg, 0x3E)
        self._write(constants.TReloadRegL, 30)
        self._write(constants.TReloadRegH, 0)

        self._write(constants.TxAutoReg, 0x40)
        self._write(constants.ModeReg, 0x3D)
        self._antenna_on()

    def __del__(self):
        self._close()

    def _reset(self):
        self._write(constants.CommandReg, constants.PCD_RESETPHASE)

    def _write(self, addr: int, val: int) -> None:
        val = self.spi.xfer2([(addr << 1) & 0x7E, val])

    def _read(self, addr: int) -> int:
        val = self.spi.xfer2([((addr << 1) & 0x7E) | 0x80, 0])
        return val[1]

    def _close(self) -> None:
        self.spi.close()
        self._reset_pin_line.release()

    def _set_bit_mask(self, reg: int, mask: int) -> None:
        tmp = self._read(reg)
        self._write(reg, tmp | mask)

    def _clear_bit_mask(self, reg: int, mask: int) -> None:
        tmp = self._read(reg)
        self._write(reg, tmp & (~mask))

    def _antenna_on(self) -> None:
        temp = self._read(constants.TxControlReg)
        if ~(temp & 0x03):
            self._set_bit_mask(constants.TxControlReg, 0x03)

    def _antenna_off(self) -> None:
        self._clear_bit_mask(constants.TxControlReg, 0x03)

    def _check_bcc(self, data: List[int]) -> bool:
        checksum = 0
        for datum in data[:-1]:
            checksum = checksum ^ datum
        return checksum == data[-1]

    def _combine_uid(self, uid_per_level: List[List[int]]) -> List[int]:
        if len(uid_per_level) == 1:
            return uid_per_level[0][:4]
        elif len(uid_per_level) == 2:
            return [*uid_per_level[0][1:4], *uid_per_level[1][:4]]
        elif len(uid_per_level) == 3:
            return [
                *uid_per_level[0][1:4],
                *uid_per_level[1][1:4],
                *uid_per_level[2][:4],
            ]

        raise RuntimeError

    def _append_crc(self, data: List[int]) -> List[int]:
        data.extend(self._calculate_crc(data))
        return data

    def _calculate_crc(self, data: List[int]) -> List[int]:
        self._clear_bit_mask(constants.DivIrqReg, 0x04)
        self._set_bit_mask(constants.FIFOLevelReg, 0x80)

        for i in range(len(data)):
            self._write(constants.FIFODataReg, data[i])

        self._write(constants.CommandReg, constants.PCD_CALCCRC)
        i = 0xFF

        while True:
            n = self._read(constants.DivIrqReg)
            i -= 1
            if not ((i != 0) and not (n & 0x04)):
                break

        return [
            self._read(constants.CRCResultRegL),
            self._read(constants.CRCResultRegM),
        ]

    def _stop_crypto1(self):
        self._clear_bit_mask(constants.Status2Reg, 0x08)

    def _to_card(self, command: int, send_data: List[int]) -> Tuple[List[int], int]:
        back_data = []
        back_len = 0
        status = constants.MI_ERR
        irq_en = 0x00
        wait_irq = 0x00
        last_bits = None
        n = 0

        if command == constants.PCD_AUTHENT:
            irq_en = 0x12
            wait_irq = 0x10
        if command == constants.PCD_TRANSCEIVE:
            irq_en = 0x77
            wait_irq = 0x30

        self._write(constants.CommIEnReg, irq_en | 0x80)
        self._clear_bit_mask(constants.CommIrqReg, 0x80)
        self._set_bit_mask(constants.FIFOLevelReg, 0x80)

        self._write(constants.CommandReg, constants.PCD_IDLE)

        for i in range(len(send_data)):
            self._write(constants.FIFODataReg, send_data[i])

        self._write(constants.CommandReg, command)

        if command == constants.PCD_TRANSCEIVE:
            self._set_bit_mask(constants.BitFramingReg, 0x80)

        i = 2000
        while True:
            n = self._read(constants.CommIrqReg)
            i -= 1
            if ~((i != 0) and ~(n & 0x01) and ~(n & wait_irq)):
                break

        self._clear_bit_mask(constants.BitFramingReg, 0x80)

        if i != 0:
            error_reg = self._read(constants.ErrorReg)
            if (error_reg & 0x1B) == 0x00:
                status = constants.MI_OK

                if n & irq_en & 0x01:
                    status = constants.MI_NOTAGERR

                if command == constants.PCD_TRANSCEIVE:
                    n = self._read(constants.FIFOLevelReg)
                    last_bits = self._read(constants.ControlReg) & 0x07
                    if last_bits != 0:
                        back_len = (n - 1) * 8 + last_bits
                    else:
                        back_len = n * 8

                    if n == 0:
                        n = 1
                    if n > constants.MAX_LEN:
                        n = constants.MAX_LEN

                    for i in range(n):
                        back_data.append(self._read(constants.FIFODataReg))
            else:
                status = constants.MI_ERR

        if status != constants.MI_OK:
            raise RuntimeError

        return (back_data, back_len)

    def request_tag(self) -> int:
        self._write(constants.BitFramingReg, 0x07)
        _back_data, back_bits = self._to_card(
            constants.PCD_TRANSCEIVE,
            [constants.PICC_REQIDL],
        )

        if back_bits != 0x10:
            raise RuntimeError

        return back_bits

    def select_tag(self) -> List[int]:
        # level 1 and 2 supported for now
        uid_per_level: List[List[int]] = []
        for level in range(1, 3, 1):
            if level == 1:
                select_cmd = constants.PICC_SElECTTAG
            elif level == 2:
                select_cmd = constants.PICC_SElECTTAG2
            else:
                raise RuntimeError

            if level == 1:
                self._write(constants.BitFramingReg, 0x00)

            # get part of UID of current cascade level
            back_data, _back_bits = self._to_card(
                constants.PCD_TRANSCEIVE, [select_cmd, 0x20]
            )

            if len(back_data) != 5:
                raise RuntimeError

            if not self._check_bcc(back_data):
                raise RuntimeError

            uid_per_level.append(back_data)

            # attempt to select this UID
            payload = self._append_crc([select_cmd, 0x70, *back_data])
            back_data, _back_bits = self._to_card(constants.PCD_TRANSCEIVE, payload)

            if len(back_data) != 3:
                raise RuntimeError

            sak = back_data[0]
            if (sak & 0b00000100) == 0b00000100:
                # cascade bit set, UID not complete
                pass
            elif (sak & 0b00100000) == 0b00100000:
                # uid complete, ISO 14443-4 compliant
                return self._combine_uid(uid_per_level)
            elif (sak & 0b00000000) == 0b00000000:
                # uid complete, not ISO 14443-4 compliant
                return self._combine_uid(uid_per_level)

        raise RuntimeError

    def read_block(self, block_addr: int) -> List[int]:
        recv_data = self._append_crc([constants.PICC_READ, block_addr])
        back_data, _back_len = self._to_card(constants.PCD_TRANSCEIVE, recv_data)

        if len(back_data) == 16:
            self.logger.debug("Sector " + str(block_addr) + " " + str(back_data))
            return back_data

        raise RuntimeError

    def write_block(self, block_addr: int, write_data: List[int]) -> None:
        buff = self._append_crc([0xA2, block_addr, *write_data])
        _back_data, _back_len = self._to_card(constants.PCD_TRANSCEIVE, buff)
