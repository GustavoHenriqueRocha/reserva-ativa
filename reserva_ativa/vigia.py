"""Laço principal: carrega o TOML, vigia cada serviço e expõe o estado em /status."""
import json
import logging
import shlex
import threading
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .estado import Acao, Lado, Regras, Servico
from .executor import ProcessoLocal, principal_no_ar

log = logging.getLogger("reserva-ativa")


class Vigiado:
    def __init__(self, nome, checagem, comando, pasta=None, log_processo=None, regras=None):
        self.servico = Servico(nome, regras or Regras())
        self.checagem = checagem
        self.processo = ProcessoLocal(comando, pasta, log_processo)
        self.ultima_checagem = None

    def rodada(self, agora, principal_ok):
        self.ultima_checagem = agora
        antes = len(self.servico.eventos)
        acao = self.servico.passo(agora, principal_ok, self.processo.vivo)
        for evento in self.servico.eventos[antes:]:
            log.info("%s: %s", self.servico.nome, evento)
        del self.servico.eventos[:]
        if acao is Acao.SUBIR:
            self.processo.subir()
            log.info("%s: processo local SUBIU (pid %s)", self.servico.nome, self.processo.pid)
        elif acao is Acao.DERRUBAR:
            pid = self.processo.pid
            self.processo.derrubar()
            log.info("%s: processo local DERRUBADO (pid %s)", self.servico.nome, pid)
        return acao

    def resumo(self):
        s = self.servico
        return {"nome": s.nome, "lado": s.lado.value, "processo_local": self.processo.vivo,
                "pid": self.processo.pid if self.processo.vivo else None,
                "principal_fora_desde": s.fora_desde, "quedas_locais": s.quedas_locais}


def carregar(caminho):
    dados = tomllib.loads(Path(caminho).read_text(encoding="utf-8"))
    geral = dados.get("geral", {})
    vigiados = []
    for s in dados["servico"]:
        # cada regra vale do [[servico]]; se ele não disser, do [geral]; senão, o padrão
        regras = Regras(**{k: s.get(k, geral.get(k)) for k in Regras.__dataclass_fields__
                           if k in s or k in geral})
        comando = s["comando"] if isinstance(s["comando"], list) else shlex.split(s["comando"])
        vigiados.append(Vigiado(s["nome"], s["checagem"], comando, s.get("pasta"), s.get("log"), regras))
    return geral, vigiados


def servir_status(vigiados, host, porta):
    class Atendente(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            if self.path != "/status":
                self.send_error(404)
                return
            corpo = json.dumps([v.resumo() for v in vigiados], ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(corpo)

    servidor = ThreadingHTTPServer((host, porta), Atendente)
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    return servidor


def rodar(vigiados, intervalo=10.0, parar=None):
    """Checa todos em paralelo (um principal lento não atrasa os outros) e age em sequência."""
    parar = parar or threading.Event()
    with ThreadPoolExecutor(max_workers=max(1, len(vigiados))) as pool:
        while not parar.is_set():
            inicio = time.monotonic()
            resultados = list(pool.map(lambda v: principal_no_ar(v.checagem), vigiados))
            agora = time.monotonic()
            for vigiado, ok in zip(vigiados, resultados):
                vigiado.rodada(agora, ok)
            parar.wait(max(0.0, intervalo - (time.monotonic() - inicio)))
    for v in vigiados:
        if v.servico.lado is Lado.RESERVA:
            log.info("%s: encerrando, derrubando o processo local", v.servico.nome)
        v.processo.derrubar()


def configurar_log(arquivo=None):
    formato = logging.Formatter("%(asctime)s %(message)s", "%d/%m/%Y %H:%M:%S")
    tela = logging.StreamHandler()
    tela.setFormatter(formato)
    log.addHandler(tela)
    if arquivo:
        rot = RotatingFileHandler(arquivo, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        rot.setFormatter(formato)
        log.addHandler(rot)
    log.setLevel(logging.INFO)
