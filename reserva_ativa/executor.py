"""O lado com efeito: perguntar ao principal e subir/derrubar o processo local."""
import json
import os
import signal
import socket
import subprocess
import sys
import urllib.request


def checar_http(url, metodo="GET", corpo=None, timeout=5.0, exigir_json=False):
    dados = corpo.encode() if isinstance(corpo, str) else corpo
    req = urllib.request.Request(url, data=dados, method=metodo,
                                 headers={"Content-Type": "application/json"} if dados else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            conteudo = resp.read()
            if not 200 <= resp.status < 300:
                return False
            if exigir_json:
                json.loads(conteudo)  # um proxy que devolve página de erro com 200 não conta
            return True
    except (OSError, ValueError):
        return False


def checar_tcp(host, porta, timeout=3.0):
    try:
        with socket.create_connection((host, porta), timeout=timeout):
            return True
    except OSError:
        return False


def principal_no_ar(checagem):
    """`checagem` vem do TOML. Vários endereços (ex.: LAN e VPN) contam como um só:
    basta um responder. Assim um cabo solto na rede local não vira falso alarme
    se o principal ainda responde pela VPN."""
    tipo = checagem.get("tipo", "http")
    timeout = checagem.get("timeout", 5.0)
    if tipo == "http":
        return any(checar_http(url, checagem.get("metodo", "GET"), checagem.get("corpo"),
                               timeout, checagem.get("exigir_json", False))
                   for url in checagem["urls"])
    if tipo == "tcp":
        return any(checar_tcp(host, checagem["porta"], timeout) for host in checagem["hosts"])
    raise ValueError(f"tipo de checagem desconhecido: {tipo}")


class ProcessoLocal:
    """Um processo filho que pode ser derrubado com toda a árvore dele."""

    def __init__(self, comando, pasta=None, log=None):
        self.comando = comando
        self.pasta = pasta
        self.log = log
        self.proc = None

    @property
    def vivo(self):
        return self.proc is not None and self.proc.poll() is None

    @property
    def pid(self):
        return self.proc.pid if self.proc else None

    def subir(self):
        saida = open(self.log, "a", encoding="utf-8") if self.log else subprocess.DEVNULL
        extra = {}
        if sys.platform == "win32":
            extra["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        else:
            extra["start_new_session"] = True  # grupo próprio: dá para matar os netos também
        self.proc = subprocess.Popen(self.comando, cwd=self.pasta, stdout=saida,
                                     stderr=subprocess.STDOUT, **extra)
        if saida is not subprocess.DEVNULL:
            saida.close()  # o filho herdou o descritor

    def derrubar(self, espera=5.0):
        if not self.vivo:
            return
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.proc.pid)], capture_output=True)
        else:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(espera)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            self.proc.wait(espera)
        except subprocess.TimeoutExpired:
            pass
