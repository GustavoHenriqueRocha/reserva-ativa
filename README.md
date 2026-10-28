# reserva-ativa

![testes](https://github.com/GustavoHenriqueRocha/reserva-ativa/actions/workflows/testes.yml/badge.svg)
![Python](https://img.shields.io/badge/python-3.11%2B-3776AB)
![dependências](https://img.shields.io/badge/depend%C3%AAncias-nenhuma-39d98a)

Uma segunda máquina que **assume sozinha** os serviços quando a principal cai, e **devolve** quando ela volta. Ela vigia a principal por HTTP ou TCP, sobe o mesmo processo localmente e o derruba (com toda a árvore de processos) assim que a principal se estabiliza.

Não usa IP virtual, cluster nem banco: só Python, um arquivo TOML e duas máquinas que se enxergam pela rede. Funciona em Linux e Windows.

```text
28/10/2026 03:12:40 leitor-extrusora: principal não respondeu
28/10/2026 03:13:40 leitor-extrusora: principal fora há 60 s: assumindo
28/10/2026 03:13:40 leitor-extrusora: processo local SUBIU (pid 8124)
28/10/2026 03:41:10 leitor-extrusora: principal estável (2 respostas): devolvendo
28/10/2026 03:41:10 leitor-extrusora: processo local DERRUBADO (pid 8124)
```

## Para que serve

Nasceu na leitura de máquinas de fábrica. Um servidor lê CLPs, IHMs e conversores serial/TCP, e um PC reserva precisa continuar a leitura se esse servidor travar ou perder a rede. Há duas exigências que um "reinicia se cair" ingênuo não atende:

- **Nunca dois leitores ao mesmo tempo.** Muitas IHMs e conversores aceitam um cliente só. Se a reserva continua conectada quando a principal volta, os dois brigam pela conexão e ninguém lê. Por isso a devolução é imediata e mata a árvore inteira do processo (filhos e netos), não só o processo pai.
- **Não ficar trocando de lado.** Uma rede que oscila não pode fazer a reserva subir e descer a cada rodada. Por isso a reserva só assume depois de um prazo de queda contínua e só devolve depois de várias respostas seguidas da principal.

Serve para qualquer serviço que precise de um substituto a quente com no máximo uma instância ativa: coletores, integradores, bots, filas.

## Como decide

```text
            principal fora por `assumir_apos` segundos
 PRINCIPAL ─────────────────────────────────────────────▶ RESERVA
           ◀─────────────────────────────────────────────
            principal respondeu `devolver_apos` vezes seguidas
```

- **Queda curta não dispara.** Se a principal volta antes do prazo, a contagem zera.
- **Vários endereços contam como um.** Por exemplo, a rede local e a VPN: basta um responder. Um cabo solto na rede local não vira falso alarme se a principal ainda responde pela VPN.
- **Backoff no processo local.** Se o processo da reserva cai, ele volta após 5 s, depois 10, 20, 40… até o teto. A espera zera quando ele fica de pé por tempo suficiente.
- **Faxina.** Um processo local que encontra a principal no ar (porque sobrou de antes ou foi aberto à mão) é derrubado.
- **Ao encerrar** (Ctrl+C ou SIGTERM), a reserva derruba o que tinha subido.

A lógica fica numa máquina de estados pura (`reserva_ativa/estado.py`), sem rede nem processos. Por isso dá para testar qualquer sequência de falhas com um relógio de mentira.

## Uso

```toml
[geral]
intervalo = 10          # segundos entre rodadas
assumir_apos = 60
devolver_apos = 2
log = "reserva.log"     # com rotação
status_porta = 8790     # GET /status em JSON

[[servico]]
nome = "leitor-extrusora"
comando = ["python", "servidor.py"]
pasta = "/opt/leitor-extrusora"
checagem = { tipo = "http", urls = ["http://10.0.0.5:8770/status", "http://100.64.0.5:8770/status"], exigir_json = true }

[[servico]]
nome = "leitor-dosadores"
comando = "python servidor.py --porta 8768"
pasta = "/opt/leitor-dosadores"
assumir_apos = 30       # cada serviço pode ter as próprias regras
checagem = { tipo = "tcp", hosts = ["10.0.0.5"], porta = 8768 }
```

```bash
python -m reserva_ativa --checar exemplos/reserva.toml   # testa as checagens uma vez
python -m reserva_ativa exemplos/reserva.toml            # vigia
curl http://127.0.0.1:8790/status                        # quem está atendendo agora
```

Na checagem HTTP, `exigir_json = true` evita que uma página de erro de proxy devolvida com 200 conte como "no ar". Também dá para usar `metodo = "POST"` e `corpo = "{}"`.

Para rodar como serviço, use uma unit do systemd no Linux ou uma tarefa agendada "ao iniciar" no Windows, apontando para `python -m reserva_ativa /caminho/config.toml`.

## Testes

```bash
python -m unittest discover -s tests -v
```

- **Máquina de estados:** prazo para assumir, queda curta, principal oscilando, devolução imediata, backoff dobrando até o teto e zerando, e faxina de processo que sobrou.
- **Executor:** checagens HTTP e TCP, vários endereços, e derrubada da árvore de processos, conferindo que o neto também morre.
- **Ponta a ponta:** uma principal HTTP de verdade é desligada e religada, e o teste confere que o processo local sobe, que o `/status` mostra "reserva" e que o processo é derrubado na volta.

## Licença

MIT
