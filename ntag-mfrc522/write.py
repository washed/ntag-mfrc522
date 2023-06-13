import RPi.GPIO as GPIO
from ntag215 import NTag215

ntag = NTag215()
try:
    text = input("Enter tag data:")
    print("Hold tag to module")
    ntag.write(text)
    print("Done...")
finally:
    # move this into del?
    GPIO.cleanup()
