#!/usr/bin/env python3
"""Simulador da Raspberry Pi para rodar no PC (sem hardware).

Usa a MESMA lógica do main.py (decisão, cache, fila offline, envio à API), mas no lugar do
leitor RFID você digita o número da tag, e LEDs/buzzer aparecem como texto.

Uso (com o backend rodando):
    python simulador_pc.py
Comandos:  <número da tag> | offline | online | fila | sair
Opcional:  TRABG2_API_URL (padrão http://localhost:5000) e TRABG2_API_KEY
"""
import os
import sys
import types
import uuid
from datetime import date, datetime

os.environ.setdefault("TRABG2_API_URL", "http://localhost:5000")

# --- troca RPi.GPIO e mfrc522 por versões de mentira, para importar o main.py no PC
gpio = types.ModuleType("RPi.GPIO")
for _n in ("BOARD", "OUT", "HIGH", "LOW"):
    setattr(gpio, _n, 1)
gpio.getmode = lambda: None
for _n in ("setmode", "setwarnings", "setup", "output", "cleanup"):
    setattr(gpio, _n, lambda *a, **k: None)
rpi = types.ModuleType("RPi")
rpi.GPIO = gpio
mf = types.ModuleType("mfrc522")
mf.SimpleMFRC522 = object
sys.modules.update({"RPi": rpi, "RPi.GPIO": gpio, "mfrc522": mf})

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import main as pi  # noqa: E402

CORES = {"verde": "\033[92m", "amarelo": "\033[93m", "vermelho": "\033[91m"}
BIPES = {"curto1": "1 bipe curto", "curto2": "2 bipes curtos", "longo": "1 bipe longo", "alarme": "bipes rápidos (alarme)"}


def main():
    url_real = pi.API_URL
    fila = pi.ler_json(pi.FILA_FILE, [])
    cache = {"colaboradores": []}
    estado = {"dentro": set(), "entraram_hoje": set(), "data": date.today().isoformat()}
    if not pi.sincronizar(cache, estado, inicial=True):
        cache["colaboradores"] = pi.ler_json(pi.CACHE_FILE, {}).get("colaboradores", [])
    pi.esvaziar_fila(fila)
    print(f"\nSimulador pronto (API: {url_real}). Digite o número de uma tag, 'offline', 'online', 'fila' ou 'sair'.")
    while True:
        try:
            cmd = input("\ntag> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if cmd in ("sair", "q", "exit"):
            break
        if cmd == "offline":
            pi.API_URL = "http://127.0.0.1:1"
            print("-> simulando queda de conexão (usa cache e fila).")
            continue
        if cmd == "online":
            pi.API_URL = url_real
            pi.sincronizar(cache, estado)
            pi.esvaziar_fila(fila)
            continue
        if cmd == "fila":
            print(f"-> {len(fila)} evento(s) pendente(s).")
            continue
        if not cmd.isdigit():
            print("Digite apenas o número da tag (ex.: 584186329875).")
            continue
        if date.today().isoformat() != estado["data"]:
            estado["data"] = date.today().isoformat()
            estado["entraram_hoje"].clear()
        tipo, msg, led, padrao, _ = pi.decidir(cmd, cache["colaboradores"], estado["dentro"], estado["entraram_hoje"])
        pi.aplicar_estado(estado, cmd, tipo)
        print(f"{CORES[led]}[LED {led.upper()}]\033[0m  [{BIPES[padrao]}]  {msg}  ({tipo})")
        pi.registrar({"uid": str(uuid.uuid4()), "tag": cmd, "tipo": tipo,
                      "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}, fila)


if __name__ == "__main__":
    main()
