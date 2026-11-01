# NetPulse

Monitor leve de computadores e dispositivos de uma rede, **só com a biblioteca padrão do Python** (nada para instalar). Um **agente** em cada computador informa como ele está, e um **painel web** mostra tudo em um lugar, no estilo de uma central de gerenciamento.

> Projeto de portfólio, pensado para laboratório e rede doméstica ou de pequeno escritório. Não substitui Zabbix, Grafana ou ESET Management em produção: a ideia é entender como esse tipo de ferramenta funciona por dentro.

## O que mostra

Para cada **computador com agente** (Windows ou Linux):

- Tempo **ligado** (uptime) e, quando some, há quanto tempo está **desligado**; disponibilidade em 24h e 7 dias
- **CPU**, **memória** e **todos os discos** (espaço livre e percentual)
- **Rede em uso**: Wi-Fi ou cabo, nome da rede (SSID), IP e gateway
- **Atualizações do sistema** pendentes e se precisa reiniciar
- **Aplicativos instalados** e quais têm **versão nova** (Windows via `winget`, que consulta a internet; Linux via `apt`)
- Gráficos de 24h

Para **dispositivos sem agente** (celular, TV, impressora, câmera, switch): se estão respondendo na rede (ping ou porta TCP) e a latência.

**Alertas** por Telegram, webhook ou e-mail quando algo abre ou volta ao normal, com confirmação (não avisa por um pico de 1 minuto) e sem repetir a mesma mensagem.

**Atualização remota por SSH** (opcional), com ações fixas e chave SSH.

## Como funciona

```
 PC com agente ──(HTTP + token a cada 60s)──►  Servidor ──► SQLite
 PC com agente ──────────────────────────────►     │  ├─► painel web (http://servidor:8765)
 celular/TV/impressora ◄──(ping/TCP a cada 30s)────┘  ├─► alertas (Telegram/webhook/e-mail)
                                                      └─► SSH ──► PC (ação fixa de atualização)
```

| Módulo | Função |
|---|---|
| `coletores.py` | Lê CPU, RAM, discos, uptime, rede, atualizações e aplicativos da máquina local |
| `agente.py` | Monta o relatório e envia ao servidor |
| `servidor.py` | API, painel, verificação periódica (offline/sondas) e ações remotas |
| `regras.py` | Limites, problemas, confirmação por leituras seguidas, abertura/fechamento |
| `sondas.py` | Ping e TCP para dispositivos sem agente |
| `alertas.py` | Telegram, webhook e e-mail |
| `ssh.py` | Ações fixas por SSH, só com chave |
| `db.py` | SQLite (dispositivos, métricas, problemas, ações) |

## Uso rápido

Requisitos: Python 3.9 ou superior (no Windows, `python`; no Linux, `python3`).

**1. Servidor** (no computador que ficará ligado):

```powershell
python -m netpulse servidor --host 0.0.0.0
```

Ele mostra dois tokens: o **dos agentes** e o de **administrador**. Abra `http://IP-DO-SERVIDOR:8765`.

**2. Agente** (em cada computador monitorado):

```powershell
python -m netpulse agente --servidor http://IP-DO-SERVIDOR:8765 --token TOKEN-DOS-AGENTES
```

Teste primeiro com `--uma-vez`. Para iniciar com o Windows, crie uma tarefa agendada que rode o comando acima ao fazer logon (o `winget` funciona melhor na conta do usuário do que na conta SYSTEM).

**3. Celulares, TVs, impressoras** (sem agente):

```powershell
python -m netpulse adicionar --nome "iPhone do Gabriel" --host 192.168.10.60
python -m netpulse importar data/hosts_exemplo.csv      # lista inteira (mesmo formato do projeto inventario-rede)
```

Comandos úteis: `listar`, `remover ID`, `--intervalo` (segundos entre verificações), `--dados PASTA`.

## Limites padrão (em `regras.py`)

| Item | Alerta | Crítico | Observação |
|---|---|---|---|
| Memória | 85% | 95% | só avisa após 3 leituras seguidas |
| CPU | 95% | | só após 5 leituras seguidas |
| Disco | 80% | 90% | por disco |
| Sem contato do agente | | 3 min | conta o tempo desde o último contato |
| Ping/TCP | | 3 falhas seguidas | |
| Atualizações / aplicativos | 10 ou mais | | abaixo disso, só aviso informativo |

## Alertas

Copie `data/alertas.exemplo.json` para `dados/alertas.json` e preencha só os canais que quiser. Reinicie o servidor. Uma falha em um canal não derruba os outros nem o monitor.

## Atualizar por SSH

Pensado para um laboratório. A ideia: o servidor executa, por SSH, **uma ação fixa** (`atualizar_apps`) em um computador que você cadastrou.

1. No computador alvo, tenha o servidor SSH ativo (Windows: *Recursos Opcionais > Servidor OpenSSH*; Linux: `openssh-server`). No Linux, a conta precisa de `sudo` sem senha só para o `apt-get` (`sudo visudo`).
2. No servidor NetPulse: `ssh-keygen -t ed25519` e copie a chave pública para o alvo (`ssh-copy-id`, ou `authorized_keys` no Windows).
3. Cadastre: `python -m netpulse ssh-configurar ID --host 192.168.10.20 --usuario gabriel --chave ~/.ssh/id_ed25519` (o ID aparece em `listar`) e reinicie o servidor.
4. Rode pelo terminal (`python -m netpulse atualizar ID`) ou pelo botão no painel (pede o token de administrador).

Decisões de segurança:

- Só **ações fixas**; nenhum texto digitado vira comando.
- Só **chave SSH** (`BatchMode`); nenhuma senha é guardada.
- O endereço do alvo vem do **seu** `ssh.json`, não do que o agente informa.
- Há **dois tokens**: quem tem o token dos agentes (todo PC monitorado) **não** consegue mandar ações a outros computadores.
- Uma ação por vez em cada dispositivo; o resultado fica registrado.

## Limitações (leia antes de usar)

- **Celulares:** iOS e Android não deixam um programa comum ler memória, disco ou lista de aplicativos como no PC. Hoje eles aparecem só como **presença na rede**. Dá para ir além com um app nativo ou com MDM, o que está fora deste projeto. Muitos celulares também dormem o Wi-Fi e podem parecer "desligados" por instantes: use uma reserva de IP no DHCP.
- **O painel não tem login.** Deixe em rede local de confiança e não exponha na internet. Os tokens protegem o envio de dados e as ações, mas a leitura do painel é aberta. A comunicação é HTTP simples (sem TLS): para fora da rede local, coloque atrás de um proxy HTTPS.
- **SSH a partir do painel:** só use em laboratório até revisar o código e entender o risco; o administrador do servidor passa a poder atualizar os computadores cadastrados.
- A leitura de **atualizações e aplicativos no Windows** (`winget`, `netsh`, registro) segue o formato padrão desses programas; mudanças futuras do `winget` podem exigir ajuste no parser. A lista de atualizações é consultada a cada 6 horas.
- Sem SNMP, sem mapa de topologia e sem gráficos além de 24h na v1.

## Testes

```bash
python -m unittest discover -s tests -v
```

Cobrem os leitores (Windows e Linux, com saídas de exemplo), regras e confirmação, abertura e fechamento de problemas, banco, ping/TCP, alertas, SSH (inclusive tentativas de injeção em host, usuário e ação), a API HTTP completa com os dois tokens e a CLI. Os testes não chamam `ssh` nem `winget` de verdade.

## Próximos passos (ideias)

Métricas por SNMP (switches e roteadores), mapa de topologia a partir do `inventario-rede`, login no painel, TLS e agente em Android via Termux.

## Licença

MIT, veja `LICENSE`.
