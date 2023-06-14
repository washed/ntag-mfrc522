from ntag_mfrc522.ntag215 import NTag215

ntag = NTag215()

text = input("Enter tag data:")
print("Hold tag to module")
ntag.write(text)
print("Done...")
