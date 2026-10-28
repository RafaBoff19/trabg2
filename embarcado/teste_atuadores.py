#!/usr/bin/env python3
"""Testa LEDs e buzzer, um de cada vez. Pinos físicos: verde 11, amarelo 13, vermelho 15, buzzer 12."""
import time
import RPi.GPIO as GPIO

PINOS = {"LED verde": 11, "LED amarelo": 13, "LED vermelho": 15, "Buzzer": 12}

GPIO.setmode(GPIO.BOARD)
try:
    for nome, pino in PINOS.items():
        GPIO.setup(pino, GPIO.OUT, initial=GPIO.LOW)
    for nome, pino in PINOS.items():
        print(f"Testando {nome} (pino {pino})...")
        GPIO.output(pino, GPIO.HIGH)
        time.sleep(1)
        GPIO.output(pino, GPIO.LOW)
        time.sleep(0.5)
    print("Teste concluído.")
finally:
    GPIO.cleanup()
