import logging
from typing import List

import RPi.GPIO as GPIO
import spidev
from constants import *

"""
TODO

* use exceptions instead of error returns
* use interrupt pin
* don't use magic numbers for register config
* logging
"""


class MFRC522:
    def __init__(
        self,
        bus=0,
        device=0,
        spd=1000000,
        pin_mode=10,
        pin_rst=-1,
        log_level="WARNING",
    ):
        self.spi = spidev.SpiDev()
        self.spi.open(bus, device)
        self.spi.max_speed_hz = spd

        self.logger = logging.getLogger("mfrc522Logger")
        self.logger.addHandler(logging.StreamHandler())
        level = logging.getLevelName(log_level)
        self.logger.setLevel(level)

        gpioMode = GPIO.getmode()

        if gpioMode is None:
            GPIO.setmode(pin_mode)
        else:
            pin_mode = gpioMode

        if pin_rst == -1:
            if pin_mode == 11:
                pin_rst = 15
            else:
                pin_rst = 22

        GPIO.setup(pin_rst, GPIO.OUT)
        GPIO.output(pin_rst, 1)

        # TODO: do irq pin config here

        self._init()

    def _init(self):
        self._reset()

        self._write(self.TModeReg, 0x8D)
        self._write(self.TPrescalerReg, 0x3E)
        self._write(self.TReloadRegL, 30)
        self._write(self.TReloadRegH, 0)

        self._write(self.TxAutoReg, 0x40)
        self._write(self.ModeReg, 0x3D)
        self._antenna_on()

    def _reset(self):
        self._write(self.CommandReg, self.PCD_RESETPHASE)

    def _write(self, addr, val):
        val = self.spi.xfer2([(addr << 1) & 0x7E, val])

    def _read(self, addr):
        val = self.spi.xfer2([((addr << 1) & 0x7E) | 0x80, 0])
        return val[1]

    def _close(self):
        self.spi.close()
        GPIO.cleanup()

    def _set_bit_mask(self, reg, mask):
        tmp = self._read(reg)
        self._write(reg, tmp | mask)

    def _clear_bit_mask(self, reg, mask):
        tmp = self._read(reg)
        self._write(reg, tmp & (~mask))

    def _antenna_on(self):
        temp = self._read(self.TxControlReg)
        if ~(temp & 0x03):
            self._set_bit_mask(self.TxControlReg, 0x03)

    def _antenna_off(self):
        self._clear_bit_mask(self.TxControlReg, 0x03)

    def _check_bcc(self, data: List[int]) -> bool:
        checksum = 0
        for datum in data[:-1]:
            checksum = checksum ^ datum
        return checksum == data[-1]

    def _combine_uid(self, uid_per_level: List[List[int]]):
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

    def _calculate_crc(self, pIndata):
        self._clear_bit_mask(self.DivIrqReg, 0x04)
        self._set_bit_mask(self.FIFOLevelReg, 0x80)

        for i in range(len(pIndata)):
            self._write(self.FIFODataReg, pIndata[i])

        self._write(self.CommandReg, self.PCD_CALCCRC)
        i = 0xFF
        while True:
            n = self._read(self.DivIrqReg)
            i -= 1
            if not ((i != 0) and not (n & 0x04)):
                break
        pOutData = []
        pOutData.append(self._read(self.CRCResultRegL))
        pOutData.append(self._read(self.CRCResultRegM))
        return pOutData

    def _stop_crypto1(self):
        self._clear_bit_mask(self.Status2Reg, 0x08)

    def request_tag(self, reqMode):
        status = None
        backBits = None
        TagType = []

        self._write(self.BitFramingReg, 0x07)

        TagType.append(PICC_REQIDL)
        (status, backData, backBits) = self.MFRC522_ToCard(self.PCD_TRANSCEIVE, TagType)

        if (status != self.MI_OK) | (backBits != 0x10):
            status = self.MI_ERR

        return (status, backBits)

    def select_tag(self):
        # level 1 and 2 supported for now
        uid_per_level: List[List[int]] = []
        for level in range(1, 3, 1):
            if level == 1:
                select_cmd = self.PICC_SElECTTAG
            elif level == 2:
                select_cmd = self.PICC_SElECTTAG2

            if level == 1:
                self._write(self.BitFramingReg, 0x00)

            # get part of UID of current cascade level
            payload = [select_cmd, 0x20]
            (status, backData, backBits) = self.MFRC522_ToCard(
                self.PCD_TRANSCEIVE, payload
            )

            if status != self.MI_OK:
                return (self.MI_ERR, None)

            if len(backData) != 5:
                return (self.MI_ERR, None)

            if not self._check_bcc(backData):
                return (self.MI_ERR, None)

            uid_per_level.append(backData)

            # attempt to select this UID
            payload = [select_cmd, 0x70, *backData]
            crc = self._calculate_crc(payload)
            payload.extend(crc)
            (status, backData, backBits) = self.MFRC522_ToCard(
                self.PCD_TRANSCEIVE, payload
            )

            if status != self.MI_OK:
                print("status: ", status)
                return (self.MI_ERR, None)

            if len(backData) != 3:
                return (self.MI_ERR, None)

            sak = backData[0]
            if (sak & 0b00000100) == 0b00000100:
                # cascade bit set, UID not complete
                pass
            elif (sak & 0b00100000) == 0b00100000:
                # uid complete, ISO 14443-4 compliant
                return (self.MI_OK, self._combine_uid(uid_per_level))
            elif (sak & 0b00000000) == 0b00000000:
                # uid complete, not ISO 14443-4 compliant
                return (self.MI_OK, self._combine_uid(uid_per_level))

    def read_block(self, blockAddr):
        recvData = []
        recvData.append(self.PICC_READ)
        recvData.append(blockAddr)
        pOut = self._calculate_crc(recvData)
        recvData.append(pOut[0])
        recvData.append(pOut[1])
        (status, backData, backLen) = self.MFRC522_ToCard(self.PCD_TRANSCEIVE, recvData)
        if not (status == self.MI_OK):
            self.logger.error("Error while reading!")

        if len(backData) == 16:
            self.logger.debug("Sector " + str(blockAddr) + " " + str(backData))
            return backData
        else:
            return None

    def write_block(self, blockAddr, writeData):
        buff = []
        buff.append(0xA2)
        buff.append(blockAddr)
        buff.extend(writeData)
        crc = self._calculate_crc(buff)
        buff.append(crc[0])
        buff.append(crc[1])
        (status, backData, backLen) = self.MFRC522_ToCard(self.PCD_TRANSCEIVE, buff)
        if not (status == self.MI_OK):
            status = self.MI_ERR
