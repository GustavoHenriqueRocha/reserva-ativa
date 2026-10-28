"""A decisão de assumir ou devolver, sem rede nem processos.

Cada serviço vigiado é uma máquina de estados alimentada a cada rodada com
"o principal respondeu?" e "o processo local está vivo?". Ela devolve o que
fazer: nada, subir o processo local ou derrubá-lo. Por ser pura, dá para
testar qualquer sequência de falhas com um relógio de mentira.

    PRINCIPAL ──(principal fora por `assumir_apos`)──▶ RESERVA
    RESERVA   ──(principal ok em `devolver_apos` rodadas seguidas)──▶ PRINCIPAL

A volta exige várias respostas seguidas para não ficar trocando de lado
quando o principal oscila. Mas, quando ela acontece, a reserva sai na hora:
há equipamentos (IHMs, conversores serial/TCP) que aceitam um cliente só, e
dois leitores ao mesmo tempo derrubam os dois.
"""
from dataclasses import dataclass, field
from enum import Enum


class Lado(Enum):
    PRINCIPAL = "principal"   # o principal atende; a reserva só observa
    RESERVA = "reserva"       # a reserva assumiu


class Acao(Enum):
    NADA = "nada"
    SUBIR = "subir"
    DERRUBAR = "derrubar"


@dataclass
class Regras:
    assumir_apos: float = 60.0      # segundos com o principal fora antes de assumir
    devolver_apos: int = 2          # respostas seguidas do principal antes de devolver
    espera_inicial: float = 5.0     # backoff quando o processo local cai
    espera_maxima: float = 300.0


@dataclass
class Servico:
    nome: str
    regras: Regras = field(default_factory=Regras)
    lado: Lado = Lado.PRINCIPAL
    fora_desde: float | None = None
    oks_seguidos: int = 0
    proxima_tentativa: float = 0.0
    espera_atual: float = 0.0
    quedas_locais: int = 0
    subiu: bool = False             # já subiu o processo local neste período de reserva
    eventos: list[str] = field(default_factory=list)

    def _evento(self, texto):
        self.eventos.append(texto)

    def passo(self, agora, principal_ok, local_vivo):
        """Uma rodada. Devolve a Acao que o executor deve realizar."""
        r = self.regras

        if principal_ok:
            self.oks_seguidos += 1
            if self.fora_desde is not None and self.lado is Lado.PRINCIPAL:
                self._evento("principal respondeu de novo antes do prazo")
            self.fora_desde = None if self.lado is Lado.PRINCIPAL else self.fora_desde
            if self.lado is Lado.RESERVA and self.oks_seguidos >= r.devolver_apos:
                self.lado = Lado.PRINCIPAL
                self.fora_desde = None
                self.espera_atual = 0.0
                self.subiu = False
                self._evento(f"principal estável ({self.oks_seguidos} respostas): devolvendo")
                return Acao.DERRUBAR if local_vivo else Acao.NADA
            if self.lado is Lado.PRINCIPAL and local_vivo:
                # Processo local sobrou de uma rodada anterior (ou alguém subiu à mão):
                # com o principal no ar, dois clientes não podem coexistir.
                self._evento("processo local vivo com o principal no ar: derrubando")
                return Acao.DERRUBAR
            return Acao.NADA

        self.oks_seguidos = 0
        if self.fora_desde is None:
            self.fora_desde = agora
            self._evento("principal não respondeu")

        if self.lado is Lado.PRINCIPAL:
            if agora - self.fora_desde < r.assumir_apos:
                return Acao.NADA
            self.lado = Lado.RESERVA
            self.proxima_tentativa = agora
            self._evento(f"principal fora há {agora - self.fora_desde:.0f} s: assumindo")

        # Lado RESERVA: manter o processo local de pé, com backoff se ele cair.
        if local_vivo:
            if self.espera_atual and agora - self.proxima_tentativa > r.espera_maxima:
                self.espera_atual = 0.0  # ficou de pé tempo bastante: zera o backoff
            return Acao.NADA
        if agora < self.proxima_tentativa:
            return Acao.NADA
        if self.subiu:
            self.quedas_locais += 1
            self._evento(f"processo local caiu; subindo de novo (próxima espera {min(r.espera_maxima, (self.espera_atual * 2) or r.espera_inicial):.0f} s)")
        self.subiu = True
        self.espera_atual = min(r.espera_maxima, (self.espera_atual * 2) or r.espera_inicial)
        self.proxima_tentativa = agora + self.espera_atual
        return Acao.SUBIR
