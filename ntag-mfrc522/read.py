import RPi.GPIO as GPIO
from ntag215 import NTag215

ntag = NTag215()
try:
    while True:
        id, text = ntag.read()
        # print(id)
        # print(text)
finally:
    GPIO.cleanup()
