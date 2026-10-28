"""Linha de comando: `reserva-ativa config.toml` ou `reserva-ativa checar config.toml`."""
import argparse
import signal
import sys
import threading

from . import __version__
from .executor import principal_no_ar
from .vigia import carregar, configurar_log, rodar, servir_status


def main(argv=None):
    p = argparse.ArgumentParser(prog="reserva-ativa",
                                description="Máquina reserva que assume serviços quando o principal cai.")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--checar", action="store_true", help="só testa as checagens uma vez e sai")
    p.add_argument("config")
    a = p.parse_args(argv)

    geral, vigiados = carregar(a.config)

    if a.checar:
        ok_todos = True
        for v in vigiados:
            ok = principal_no_ar(v.checagem)
            ok_todos &= ok
            print(f"{v.servico.nome:<20} principal {'no ar' if ok else 'FORA'}")
        return 0 if ok_todos else 1

    configurar_log(geral.get("log"))
    if "status_porta" in geral:
        servir_status(vigiados, geral.get("status_host", "127.0.0.1"), geral["status_porta"])

    parar = threading.Event()
    for sinal in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sinal, lambda *_: parar.set())
    nomes = ", ".join(v.servico.nome for v in vigiados)
    print(f"vigiando {nomes} a cada {geral.get('intervalo', 10)} s (Ctrl+C para sair)", flush=True)
    rodar(vigiados, geral.get("intervalo", 10.0), parar)
    return 0


if __name__ == "__main__":
    sys.exit(main())
