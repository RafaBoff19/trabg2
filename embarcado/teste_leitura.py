import time
import RPi.GPIO as GPIO
from mfrc522 import SimpleMFRC522

leitor = SimpleMFRC522()
print("Aproxime uma tag (Ctrl+C para sair)")
try:
    while True:
        tag = leitor.read_id_no_block()
        if tag is not None:
            print("ID da tag:", tag)
            time.sleep(1.5)
        time.sleep(0.1)
finally:
    GPIO.cleanup()