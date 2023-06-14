import logging
from typing import List, Optional, Tuple

import constants

import gpiod
import spidev

"""
TODO

* use exceptions instead of error returns
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

    def _calculate_crc(self, pIndata: List[int]) -> List[int]:
        self._clear_bit_mask(constants.DivIrqReg, 0x04)
        self._set_bit_mask(constants.FIFOLevelReg, 0x80)

        for i in range(len(pIndata)):
            self._write(constants.FIFODataReg, pIndata[i])

        self._write(constants.CommandReg, constants.PCD_CALCCRC)
        i = 0xFF
        while True:
            n = self._read(constants.DivIrqReg)
            i -= 1
            if not ((i != 0) and not (n & 0x04)):
                break
        pOutData = []
        pOutData.append(self._read(constants.CRCResultRegL))
        pOutData.append(self._read(constants.CRCResultRegM))
        return pOutData

    def _stop_crypto1(self):
        self._clear_bit_mask(constants.Status2Reg, 0x08)

    def _to_card(self, command: int, sendData: List[int]) -> Tuple[int, List[int], int]:
        backData = []
        backLen = 0
        status = constants.MI_ERR
        irqEn = 0x00
        waitIRq = 0x00
        lastBits = None
        n = 0

        if command == constants.PCD_AUTHENT:
            irqEn = 0x12
            waitIRq = 0x10
        if command == constants.PCD_TRANSCEIVE:
            irqEn = 0x77
            waitIRq = 0x30

        self._write(constants.CommIEnReg, irqEn | 0x80)
        self._clear_bit_mask(constants.CommIrqReg, 0x80)
        self._set_bit_mask(constants.FIFOLevelReg, 0x80)

        self._write(constants.CommandReg, constants.PCD_IDLE)

        for i in range(len(sendData)):
            self._write(constants.FIFODataReg, sendData[i])

        self._write(constants.CommandReg, command)

        if command == constants.PCD_TRANSCEIVE:
            self._set_bit_mask(constants.BitFramingReg, 0x80)

        i = 2000
        while True:
            n = self._read(constants.CommIrqReg)
            i -= 1
            if ~((i != 0) and ~(n & 0x01) and ~(n & waitIRq)):
                break

        self._clear_bit_mask(constants.BitFramingReg, 0x80)

        if i != 0:
            error_reg = self._read(constants.ErrorReg)
            if (error_reg & 0x1B) == 0x00:
                status = constants.MI_OK

                if n & irqEn & 0x01:
                    status = constants.MI_NOTAGERR

                if command == constants.PCD_TRANSCEIVE:
                    n = self._read(constants.FIFOLevelReg)
                    lastBits = self._read(constants.ControlReg) & 0x07
                    if lastBits != 0:
                        backLen = (n - 1) * 8 + lastBits
                    else:
                        backLen = n * 8

                    if n == 0:
                        n = 1
                    if n > constants.MAX_LEN:
                        n = constants.MAX_LEN

                    for i in range(n):
                        backData.append(self._read(constants.FIFODataReg))
            else:
                status = constants.MI_ERR

        return (status, backData, backLen)

    def request_tag(self) -> Tuple[int, int]:
        status = None
        backBits = None
        TagType = []

        self._write(constants.BitFramingReg, 0x07)

        TagType.append(constants.PICC_REQIDL)
        (status, _backData, backBits) = self._to_card(constants.PCD_TRANSCEIVE, TagType)

        if (status != constants.MI_OK) | (backBits != 0x10):
            status = constants.MI_ERR

        return (status, backBits)

    def select_tag(self) -> Tuple[int, Optional[List[int]]]:
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
            payload = [select_cmd, 0x20]
            (status, backData, _backBits) = self._to_card(
                constants.PCD_TRANSCEIVE, payload
            )

            if status != constants.MI_OK:
                return (constants.MI_ERR, None)

            if len(backData) != 5:
                return (constants.MI_ERR, None)

            if not self._check_bcc(backData):
                return (constants.MI_ERR, None)

            uid_per_level.append(backData)

            # attempt to select this UID
            payload = [select_cmd, 0x70, *backData]
            crc = self._calculate_crc(payload)
            payload.extend(crc)
            (status, backData, _backBits) = self._to_card(
                constants.PCD_TRANSCEIVE, payload
            )

            if status != constants.MI_OK:
                print("status: ", status)
                return (constants.MI_ERR, None)

            if len(backData) != 3:
                return (constants.MI_ERR, None)

            sak = backData[0]
            if (sak & 0b00000100) == 0b00000100:
                # cascade bit set, UID not complete
                pass
            elif (sak & 0b00100000) == 0b00100000:
                # uid complete, ISO 14443-4 compliant
                return (constants.MI_OK, self._combine_uid(uid_per_level))
            elif (sak & 0b00000000) == 0b00000000:
                # uid complete, not ISO 14443-4 compliant
                return (constants.MI_OK, self._combine_uid(uid_per_level))

        raise RuntimeError

    def read_block(self, blockAddr: int) -> Optional[List[int]]:
        recvData = []
        recvData.append(constants.PICC_READ)
        recvData.append(blockAddr)
        pOut = self._calculate_crc(recvData)
        recvData.append(pOut[0])
        recvData.append(pOut[1])
        (status, backData, _backLen) = self._to_card(constants.PCD_TRANSCEIVE, recvData)
        if not (status == constants.MI_OK):
            self.logger.error("Error while reading!")

        if len(backData) == 16:
            self.logger.debug("Sector " + str(blockAddr) + " " + str(backData))
            return backData
        else:
            return None

    def write_block(self, blockAddr: int, writeData: List[int]) -> None:
        buff = []
        buff.append(0xA2)
        buff.append(blockAddr)
        buff.extend(writeData)
        crc = self._calculate_crc(buff)
        buff.append(crc[0])
        buff.append(crc[1])
        (status, _backData, _backLen) = self._to_card(constants.PCD_TRANSCEIVE, buff)
        if not (status == constants.MI_OK):
            status = constants.MI_ERR  # lol
