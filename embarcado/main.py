#!/usr/bin/env python3
"""Aplicação embarcada (Raspberry Pi) do projeto trabg2.

Lê o crachá RFID (RC522), valida o acesso, aciona LEDs e buzzer e envia o evento
à API. Sem conexão, usa o cache local e guarda os eventos em uma fila para reenviar.

Configuração por variáveis de ambiente (ou edite os valores abaixo):
    TRABG2_API_URL  ex.: http://192.168.43.10:5000   (IP do PC que roda o backend)
    TRABG2_API_KEY  deve ser igual à do backend

Pinos dos atuadores em numeração FÍSICA (BOARD), a mesma usada pelo RC522:
    LED verde = 11 | LED amarelo = 13 | LED vermelho = 15 | buzzer ATIVO = 12
"""
import json
import os
import time
import uuid
from datetime import date, datetime

import requests
import RPi.GPIO as GPIO
from mfrc522 import SimpleMFRC522

API_URL = os.environ.get("TRABG2_API_URL", "http://192.168.0.10:5000").rstrip("/")
API_KEY = os.environ.get("TRABG2_API_KEY", "trabg2-pi-key")
LED_VERDE, LED_AMARELO, LED_VERMELHO, BUZZER = 11, 13, 15, 12
TIMEOUT = 3               # segundos de espera pela API
INTERVALO_SYNC = 60       # a cada quantos segundos tenta atualizar o cache / reenviar a fila
TEMPO_SINAL = 1.5         # tempo que o LED fica aceso após uma leitura

PASTA = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(PASTA, "cache.json")
FILA_FILE = os.path.join(PASTA, "eventos_pendentes.json")
HEADERS = {"X-API-Key": API_KEY}


# ------------------------------------------------------------ arquivos locais
def ler_json(caminho, padrao):
    try:
        with open(caminho, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return padrao


def gravar_json(caminho, dados):
    tmp = caminho + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False)
    os.replace(tmp, caminho)  # troca atômica: não corrompe se faltar energia


# ------------------------------------------------------------ decisão (lógica pura)
def decidir(tag, colaboradores, dentro, entraram_hoje):
    """Devolve (tipo, mensagem, led, padrao_buzzer, colaborador)."""
    c = next((x for x in colaboradores if str(x["tag"]) == tag), None)
    if c is None:
        return "TENTATIVA_INVASAO", "ALERTA: tag desconhecida", "vermelho", "alarme", None
    if not c["ativo"] or not c["acesso"]:
        return "ACESSO_NEGADO", f"Acesso negado, {c['nome']}", "amarelo", "longo", c
    if tag in dentro:
        return "SAIDA", f"Até logo, {c['nome']}", "verde", "curto1", c
    if tag in entraram_hoje:
        return "ENTRADA", f"Bem-vindo de volta, {c['nome']}", "verde", "curto2", c
    return "ENTRADA", f"Bem-vindo, {c['nome']}", "verde", "curto1", c


def aplicar_estado(estado, tag, tipo):
    if tipo == "ENTRADA":
        estado["dentro"].add(tag)
        estado["entraram_hoje"].add(tag)
    elif tipo == "SAIDA":
        estado["dentro"].discard(tag)


# ------------------------------------------------------------ atuadores
class Atuadores:
    LEDS = {"verde": LED_VERDE, "amarelo": LED_AMARELO, "vermelho": LED_VERMELHO}

    def __init__(self):
        if GPIO.getmode() is None:
            GPIO.setmode(GPIO.BOARD)
        GPIO.setwarnings(False)
        for pino in (*self.LEDS.values(), BUZZER):
            GPIO.setup(pino, GPIO.OUT, initial=GPIO.LOW)

    @staticmethod
    def _bipe(duracao):
        GPIO.output(BUZZER, GPIO.HIGH)
        time.sleep(duracao)
        GPIO.output(BUZZER, GPIO.LOW)

    def sinalizar(self, led, padrao):
        inicio = time.time()
        GPIO.output(self.LEDS[led], GPIO.HIGH)
        if padrao == "curto1":
            self._bipe(0.15)
        elif padrao == "curto2":
            for _ in range(2):
                self._bipe(0.12)
                time.sleep(0.12)
        elif padrao == "longo":
            self._bipe(0.8)
        elif padrao == "alarme":
            for _ in range(6):
                self._bipe(0.1)
                time.sleep(0.1)
        resto = TEMPO_SINAL - (time.time() - inicio)
        if resto > 0:
            time.sleep(resto)
        GPIO.output(self.LEDS[led], GPIO.LOW)

    def desligar(self):
        for pino in (*self.LEDS.values(), BUZZER):
            GPIO.output(pino, GPIO.LOW)


# ------------------------------------------------------------ comunicação com a API
def baixar_dados():
    r1 = requests.get(f"{API_URL}/api/colaboradores", headers=HEADERS, timeout=TIMEOUT)
    r1.raise_for_status()
    r2 = requests.get(f"{API_URL}/api/estado", headers=HEADERS, timeout=TIMEOUT)
    r2.raise_for_status()
    return r1.json(), r2.json()


def sincronizar(cache, estado, inicial=False):
    """Atualiza o cache de colaboradores. No início, também o estado (quem está dentro)."""
    try:
        colaboradores, est = baixar_dados()
    except (requests.RequestException, ValueError) as e:
        print(f"[offline] sem conexão com a API ({type(e).__name__}); usando o cache local.")
        return False
    cache["colaboradores"] = colaboradores
    if inicial:
        estado["dentro"] = set(est.get("dentro", []))
        estado["entraram_hoje"] = set(est.get("entraram_hoje", []))
    gravar_json(CACHE_FILE, {"colaboradores": colaboradores, "data": date.today().isoformat(),
                             "dentro": sorted(estado["dentro"]),
                             "entraram_hoje": sorted(estado["entraram_hoje"])})
    print(f"[sync] {len(colaboradores)} colaboradores carregados da API.")
    return True


def enviar(evento):
    """True se a API aceitou (ou se o evento é inválido e deve ser descartado)."""
    try:
        r = requests.post(f"{API_URL}/api/eventos", json=evento, headers=HEADERS, timeout=TIMEOUT)
    except requests.RequestException:
        return False
    if r.status_code in (200, 201):
        return True
    if r.status_code == 400:
        print(f"[aviso] evento rejeitado pela API e descartado: {evento}")
        return True
    return False


def registrar(evento, fila):
    """Tenta enviar agora; se falhar, vai para a fila local."""
    if fila or not enviar(evento):
        fila.append(evento)
        gravar_json(FILA_FILE, fila)
        print(f"[fila] evento guardado ({len(fila)} pendente(s)).")
        return False
    return True


def esvaziar_fila(fila):
    enviados = 0
    while fila:
        if not enviar(fila[0]):
            break
        fila.pop(0)
        enviados += 1
    if enviados:
        gravar_json(FILA_FILE, fila)
        print(f"[fila] {enviados} evento(s) reenviado(s); restam {len(fila)}.")


# ------------------------------------------------------------ programa principal
def main():
    reader = SimpleMFRC522()      # define o modo de numeração dos pinos (BOARD)
    atuadores = Atuadores()
    fila = ler_json(FILA_FILE, [])
    cache = {"colaboradores": []}
    estado = {"dentro": set(), "entraram_hoje": set(), "data": date.today().isoformat()}

    if not sincronizar(cache, estado, inicial=True):
        guardado = ler_json(CACHE_FILE, {})
        cache["colaboradores"] = guardado.get("colaboradores", [])
        if guardado.get("data") == estado["data"]:
            estado["dentro"] = set(guardado.get("dentro", []))
            estado["entraram_hoje"] = set(guardado.get("entraram_hoje", []))
        print(f"[cache] {len(cache['colaboradores'])} colaboradores do cache local.")
    esvaziar_fila(fila)

    print("Sistema pronto. Aproxime um crachá (Ctrl+C para sair).")
    ultimo_sync, ultima_tag, ultimo_instante = time.time(), None, 0.0
    try:
        while True:
            agora = time.time()
            if date.today().isoformat() != estado["data"]:      # virou o dia
                estado["data"] = date.today().isoformat()
                estado["entraram_hoje"].clear()
            if agora - ultimo_sync >= INTERVALO_SYNC:
                sincronizar(cache, estado)
                esvaziar_fila(fila)
                ultimo_sync = time.time()

            tag_id = reader.read_id_no_block()
            if tag_id is None:
                time.sleep(0.1)
                continue
            tag = str(tag_id)
            if tag == ultima_tag and agora - ultimo_instante < 3:
                time.sleep(0.1)
                continue
            ultima_tag, ultimo_instante = tag, agora

            tipo, mensagem, led, padrao, _ = decidir(
                tag, cache["colaboradores"], estado["dentro"], estado["entraram_hoje"])
            print(f"[{datetime.now():%H:%M:%S}] tag {tag}: {mensagem}  ({tipo})")
            aplicar_estado(estado, tag, tipo)
            evento = {"uid": str(uuid.uuid4()), "tag": tag, "tipo": tipo,
                      "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
            registrar(evento, fila)
            atuadores.sinalizar(led, padrao)
    except KeyboardInterrupt:
        print("\nEncerrando...")
    finally:
        GPIO.cleanup()


if __name__ == "__main__":
    main()
