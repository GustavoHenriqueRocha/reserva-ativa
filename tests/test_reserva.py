import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from reserva_ativa.estado import Acao, Lado, Regras, Servico
from reserva_ativa.executor import ProcessoLocal, checar_http, checar_tcp, principal_no_ar
from reserva_ativa.vigia import Vigiado, carregar, rodar, servir_status

RAIZ = Path(__file__).resolve().parent.parent
FORA, OK = False, True


class Simulacao:
    """Roda a máquina de estados com relógio de mentira e um 'processo' de mentira."""

    def __init__(self, **regras):
        self.s = Servico("teste", Regras(**regras))
        self.t = 0.0
        self.vivo = False
        self.acoes = []

    def rodada(self, principal, avanca=10.0):
        acao = self.s.passo(self.t, principal, self.vivo)
        if acao is Acao.SUBIR:
            self.vivo = True
        elif acao is Acao.DERRUBAR:
            self.vivo = False
        self.acoes.append(acao)
        self.t += avanca
        return acao


class TestMaquinaDeEstados(unittest.TestCase):
    def test_principal_no_ar_nao_faz_nada(self):
        sim = Simulacao()
        for _ in range(10):
            self.assertIs(sim.rodada(OK), Acao.NADA)
        self.assertIs(sim.s.lado, Lado.PRINCIPAL)

    def test_assume_so_depois_do_prazo(self):
        sim = Simulacao(assumir_apos=60)
        acoes = [sim.rodada(FORA) for _ in range(7)]  # t = 0, 10, ..., 60
        self.assertEqual(acoes[:6], [Acao.NADA] * 6)
        self.assertIs(acoes[6], Acao.SUBIR)
        self.assertIs(sim.s.lado, Lado.RESERVA)

    def test_queda_curta_nao_dispara(self):
        sim = Simulacao(assumir_apos=60)
        for principal in [FORA, FORA, FORA, OK, FORA, FORA, FORA, OK]:
            sim.rodada(principal)
        self.assertNotIn(Acao.SUBIR, sim.acoes)

    def test_devolve_so_com_respostas_seguidas(self):
        sim = Simulacao(assumir_apos=0, devolver_apos=3)
        sim.rodada(FORA)
        self.assertTrue(sim.vivo)
        # o principal oscila: nunca 3 respostas seguidas, então a reserva fica
        for principal in [OK, OK, FORA, OK, FORA, OK, OK, FORA]:
            sim.rodada(principal)
        self.assertTrue(sim.vivo)
        for _ in range(3):
            sim.rodada(OK)
        self.assertFalse(sim.vivo)
        self.assertIs(sim.s.lado, Lado.PRINCIPAL)

    def test_devolucao_derruba_na_hora(self):
        sim = Simulacao(assumir_apos=0, devolver_apos=1)
        sim.rodada(FORA)
        self.assertIs(sim.rodada(OK), Acao.DERRUBAR)

    def test_processo_local_que_cai_volta_com_backoff(self):
        sim = Simulacao(assumir_apos=0, espera_inicial=5, espera_maxima=40)
        subidas = []
        for _ in range(200):
            if sim.rodada(FORA, avanca=1.0) is Acao.SUBIR:
                subidas.append(sim.t - 1)
            sim.vivo = False  # o processo morre logo depois de subir
        intervalos = [b - a for a, b in zip(subidas, subidas[1:])]
        self.assertEqual(intervalos[:5], [5, 10, 20, 40, 40])  # dobra até o teto
        self.assertTrue(all(i <= 40 for i in intervalos))
        self.assertEqual(sim.s.quedas_locais, len(subidas) - 1)

    def test_backoff_zera_se_o_processo_fica_de_pe(self):
        sim = Simulacao(assumir_apos=0, espera_inicial=5, espera_maxima=40)
        sim.rodada(FORA)                        # sobe (próxima espera: 5)
        for _ in range(3):                      # cai três vezes: a espera cresce
            sim.vivo = False
            sim.t = sim.s.proxima_tentativa
            sim.rodada(FORA)
        self.assertEqual(sim.s.espera_atual, 40)
        for _ in range(10):                     # depois fica 100 s de pé
            sim.rodada(FORA)
        sim.vivo = False
        sim.rodada(FORA)
        self.assertEqual(sim.s.espera_atual, 5)  # recomeçou do início

    def test_processo_sobrando_com_principal_no_ar_e_derrubado(self):
        sim = Simulacao()
        sim.vivo = True  # alguém subiu à mão, ou sobrou de antes
        self.assertIs(sim.rodada(OK), Acao.DERRUBAR)

    def test_novo_periodo_de_reserva_comeca_sem_backoff(self):
        sim = Simulacao(assumir_apos=0, devolver_apos=1, espera_inicial=5)
        sim.rodada(FORA)
        sim.vivo = False
        sim.rodada(FORA, avanca=5)
        sim.rodada(OK)  # devolve
        self.assertIs(sim.rodada(FORA), Acao.SUBIR)  # assume de novo, imediatamente
        self.assertEqual(sim.s.espera_atual, 5)


class Principal:
    """Um servidor HTTP que liga e desliga, fazendo o papel da máquina principal."""

    def __init__(self):
        self.ligado = True

        dono = self

        class Atendente(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                if not dono.ligado:
                    self.send_error(503)
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"ok": true}')

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Atendente)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.http.server_address[1]}/status"

    def fechar(self):
        self.http.shutdown()
        self.http.server_close()


class TestExecutor(unittest.TestCase):
    def setUp(self):
        self.principal = Principal()

    def tearDown(self):
        self.principal.fechar()

    def test_checagem_http(self):
        self.assertTrue(checar_http(self.principal.url, exigir_json=True))
        self.principal.ligado = False
        self.assertFalse(checar_http(self.principal.url))

    def test_basta_um_endereco_responder(self):
        morto = "http://127.0.0.1:9/status"
        self.assertTrue(principal_no_ar({"tipo": "http", "urls": [morto, self.principal.url], "timeout": 1}))
        self.assertFalse(principal_no_ar({"tipo": "http", "urls": [morto], "timeout": 1}))

    def test_checagem_tcp(self):
        porta = self.principal.http.server_address[1]
        self.assertTrue(checar_tcp("127.0.0.1", porta))
        self.assertFalse(checar_tcp("127.0.0.1", 9, timeout=0.5))

    def test_derrubar_mata_a_arvore_inteira(self):
        # o filho cria um neto; derrubar tem que levar os dois
        neto = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); time.sleep(60)"
        p = ProcessoLocal([sys.executable, "-c", neto])
        p.subir()
        time.sleep(0.5)
        p.derrubar()
        self.assertFalse(p.vivo)
        if sys.platform != "win32":
            import os
            import signal
            with self.assertRaises(ProcessLookupError):
                os.killpg(p.pid, 0)


class TestPontaAPonta(unittest.TestCase):
    def test_assume_e_devolve_com_processo_de_verdade(self):
        principal = Principal()
        vigiado = Vigiado("leitor", {"tipo": "http", "urls": [principal.url], "timeout": 1},
                          [sys.executable, "-c", "import time; time.sleep(60)"],
                          regras=Regras(assumir_apos=0.3, devolver_apos=2))
        status = servir_status([vigiado], "127.0.0.1", 0)
        parar = threading.Event()
        laco = threading.Thread(target=rodar, args=([vigiado], 0.1, parar))
        laco.start()
        try:
            time.sleep(0.5)
            self.assertFalse(vigiado.processo.vivo)

            principal.ligado = False
            self.assertTrue(_espera(lambda: vigiado.processo.vivo))
            url = f"http://127.0.0.1:{status.server_address[1]}/status"
            with urllib.request.urlopen(url) as r:
                self.assertEqual(json.load(r)[0]["lado"], "reserva")

            principal.ligado = True
            self.assertTrue(_espera(lambda: not vigiado.processo.vivo))
            self.assertIs(vigiado.servico.lado, Lado.PRINCIPAL)
        finally:
            parar.set()
            laco.join(5)
            status.shutdown()
            status.server_close()
            principal.fechar()
        self.assertFalse(vigiado.processo.vivo)

    def test_exemplo_carrega(self):
        geral, vigiados = carregar(RAIZ / "exemplos" / "reserva.toml")
        self.assertEqual([v.servico.nome for v in vigiados], ["leitor-extrusora", "leitor-dosadores"])
        self.assertEqual(vigiados[1].servico.regras.assumir_apos, 30)   # sobrescrito no serviço
        self.assertEqual(vigiados[0].servico.regras.assumir_apos, 60)   # herdado do [geral]
        self.assertEqual(vigiados[1].processo.comando, ["python", "servidor.py", "--porta", "8768"])


def _espera(condicao, limite=5.0):
    fim = time.monotonic() + limite
    while time.monotonic() < fim:
        if condicao():
            return True
        time.sleep(0.05)
    return False


if __name__ == "__main__":
    unittest.main()
