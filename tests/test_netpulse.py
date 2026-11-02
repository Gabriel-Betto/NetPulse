"""Testes do NetPulse. Rodar com:  python -m unittest discover -s tests -v"""

import json
import socket
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from netpulse import alertas, cli, coletores, parsers_csv, regras, servidor, sondas, ssh
from netpulse.db import Banco

G = 1024 ** 3


class Relogio:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def avancar(self, s):
        self.t += s


def relatorio(cpu=10, ram=40, discos=None, uptime=3600, pendentes=0, reiniciar=False, apps=None, host="PC-A", so="windows"):
    discos = [("C:\\", 50)] if discos is None else discos
    return {
        "id": "abcdef1234567890", "hostname": host, "so": so, "so_versao": "x", "versao_agente": "1.0.0", "cpu": cpu,
        "ram": {"percentual": ram, "total": 16 * G, "usada": int(16 * G * ram / 100)},
        "discos": [{"ponto": p, "percentual": pc, "total": 500 * G, "livre": int(500 * G * (100 - pc) / 100)} for p, pc in discos],
        "uptime_s": uptime, "rede": {"tipo": "wifi", "interface": "Wi-Fi", "ip": "192.168.10.50", "gateway": "192.168.10.1", "ssid": "Casa"},
        "atualizacoes": {"pendentes": pendentes, "reiniciar": reiniciar},
        "aplicativos": {"total": 3, "instalados": [{"nome": "A", "versao": "1"}], "atualizaveis": apps if apps is not None else []},
    }


class TestColetores(unittest.TestCase):
    def test_meminfo(self):
        r = coletores.parse_meminfo("MemTotal: 16000000 kB\nMemFree: 1 kB\nMemAvailable: 4000000 kB\n")
        self.assertEqual(r["percentual"], 75.0)
        self.assertIsNone(coletores.parse_meminfo("lixo"))

    def test_cpu(self):
        a = coletores.parse_cpu_stat("cpu  100 0 100 700 100 0 0 0\ncpu0 1 1 1 1")
        b = coletores.parse_cpu_stat("cpu  200 0 200 750 150 0 0 0\n")
        self.assertEqual(coletores.percentual_cpu(a, b), 66.7)   # intervalo: 200 ocupados de 300
        self.assertIsNone(coletores.percentual_cpu(a, a))
        self.assertIsNone(coletores.parse_cpu_stat("nada"))

    def test_mounts(self):
        texto = ("/dev/sda1 / ext4 rw 0 0\ntmpfs /run tmpfs rw 0 0\n/dev/sdb1 /dados ext4 rw 0 0\n"
                 "/dev/sdb1 /dados2 ext4 rw 0 0\n/dev/loop1 /snap/core ext4 ro 0 0\noverlay /var overlay rw 0 0\n")
        self.assertEqual(coletores.parse_mounts(texto), ["/", "/dados"])

    def test_apt(self):
        saida = ("Listing...\ncurl/jammy-updates 7.81 amd64 [upgradable from: 7.80]\n"
                 "git/jammy-updates 1:2.34 amd64 [upgradable from: 1:2.30]\n")
        self.assertEqual(coletores.parse_apt_upgradable(saida), 2)
        lista = coletores.parse_apt_lista(saida)
        self.assertEqual([a["nome"] for a in lista], ["curl", "git"])
        self.assertEqual(lista[0]["versao"], "7.80")
        self.assertEqual(lista[0]["disponivel"], "7.81")

    def test_winget(self):
        saida = ("Nome                 ID              Versão      Disponível  Origem\n"
                 "-------------------------------------------------------------------\n"
                 "Mozilla Firefox      Mozilla.Firefox 130.0       131.0       winget\n"
                 "Git                  Git.Git         2.45.0      2.47.0      winget\n"
                 "2 atualizações disponíveis.\n")
        apps = coletores.parse_winget_upgrade(saida)
        self.assertEqual([(a["nome"], a["versao"], a["disponivel"]) for a in apps],
                         [("Mozilla Firefox", "130.0", "131.0"), ("Git", "2.45.0", "2.47.0")])
        self.assertEqual(coletores.parse_winget_upgrade("Nenhuma atualização disponível."), [])
        self.assertEqual(coletores.parse_winget_upgrade(""), [])

    def test_rede(self):
        r = coletores.parse_ip_route_get("1.1.1.1 via 192.168.10.1 dev wlan0 src 192.168.10.50 uid 0")
        self.assertEqual(r, {"interface": "wlan0", "ip": "192.168.10.50", "gateway": "192.168.10.1"})
        self.assertEqual(coletores.parse_netsh_wlan("  Nome : Wi-Fi\n  SSID                   : Casa Gabriel \n  BSSID : aa:bb"), "Casa Gabriel")
        self.assertIsNone(coletores.parse_netsh_wlan("  Nome : Wi-Fi\n  Estado : desconectado"))

    def test_rota_windows(self):
        self.assertEqual(coletores.parse_rota_windows("192.168.1.1|Ethernet\r\n"), {"gateway": "192.168.1.1", "interface": "Ethernet"})
        self.assertEqual(coletores.parse_rota_windows("|"), {"gateway": None, "interface": None})
        self.assertEqual(coletores.parse_rota_windows(""), {"gateway": None, "interface": None})

    def test_versao_do_agente_vem_do_pacote(self):
        from netpulse import __version__
        self.assertEqual(coletores.VERSAO_AGENTE, __version__)

    def test_cache_so_refaz_depois_do_ttl(self):
        chamadas, tempo = [], [0.0]
        cache = coletores.CacheAtualizacoes(ttl_s=100, buscar=lambda: chamadas.append(1) or {"n": len(chamadas)},
                                            relogio=lambda: tempo[0])
        cache.obter(); cache.obter()
        self.assertEqual(len(chamadas), 1)
        tempo[0] = 100
        self.assertEqual(cache.obter(), {"n": 2})

    def test_coletar_tem_os_campos_esperados(self):
        r = coletores.coletar(amostra_cpu_s=0.05,
                              cache=coletores.CacheAtualizacoes(buscar=lambda: {"pendentes": 0, "reiniciar": False}),
                              cache_apps=coletores.CacheAtualizacoes(buscar=lambda: {"total": 0, "instalados": [], "atualizaveis": []}))
        for chave in ("hostname", "so", "cpu", "ram", "discos", "uptime_s", "rede", "atualizacoes", "aplicativos"):
            self.assertIn(chave, r)


class TestRegras(unittest.TestCase):
    def codigos(self, rel, **lim):
        return {p.codigo: p.gravidade for p in regras.avaliar_relatorio(rel, lim)}

    def test_tudo_normal(self):
        self.assertEqual(self.codigos(relatorio()), {})

    def test_limites_de_ram_disco_cpu(self):
        self.assertEqual(self.codigos(relatorio(ram=85))["ram"], regras.ALERTA)
        self.assertEqual(self.codigos(relatorio(ram=95))["ram"], regras.CRITICO)
        self.assertNotIn("ram", self.codigos(relatorio(ram=84.9)))
        self.assertEqual(self.codigos(relatorio(discos=[("C:\\", 80)]))["disco:C:\\"], regras.ALERTA)
        self.assertEqual(self.codigos(relatorio(discos=[("C:\\", 90)]))["disco:C:\\"], regras.CRITICO)
        self.assertEqual(self.codigos(relatorio(cpu=96))["cpu"], regras.ALERTA)

    def test_atualizacoes_e_apps(self):
        self.assertEqual(self.codigos(relatorio(pendentes=3))["atualizacoes"], regras.INFO)
        self.assertEqual(self.codigos(relatorio(pendentes=10))["atualizacoes"], regras.ALERTA)
        apps = [{"nome": f"A{i}", "id": "x", "versao": "1", "disponivel": "2"} for i in range(2)]
        self.assertEqual(self.codigos(relatorio(apps=apps))["apps"], regras.INFO)
        self.assertEqual(self.codigos(relatorio(apps=apps * 5))["apps"], regras.ALERTA)
        self.assertEqual(self.codigos(relatorio(reiniciar=True))["reiniciar"], regras.INFO)

    def test_dado_ausente_nao_gera_problema(self):
        self.assertEqual(regras.avaliar_relatorio({"cpu": None, "ram": None, "discos": None, "atualizacoes": None}), [])

    def test_status(self):
        P = regras.Problema
        self.assertEqual(regras.status_de([]), "ok")
        self.assertEqual(regras.status_de([P("a", regras.INFO, "")]), "info")
        self.assertEqual(regras.status_de([P("a", regras.INFO, ""), P("b", regras.ALERTA, "")]), "alerta")
        self.assertEqual(regras.status_de([P("a", regras.CRITICO, "")]), "critico")
        self.assertEqual(regras.status_de([P("ram", regras.CRITICO, ""), P("offline", regras.CRITICO, "")]), "offline")

    def test_confirmacao_exige_leituras_seguidas(self):
        av = regras.Avaliador()
        alto = [regras.Problema("ram", regras.ALERTA, "x", confirmar=3)]
        self.assertEqual(av.confirmar("d", alto), [])
        self.assertEqual(av.confirmar("d", alto), [])
        self.assertEqual(len(av.confirmar("d", alto)), 1)
        av.confirmar("d", [])                     # voltou ao normal: zera a contagem
        self.assertEqual(av.confirmar("d", alto), [])

    def test_confirmacao_e_por_dispositivo(self):
        av = regras.Avaliador()
        alto = [regras.Problema("ram", regras.ALERTA, "x", confirmar=2)]
        av.confirmar("a", alto)
        self.assertEqual(av.confirmar("b", alto), [])


class TestSincronizar(unittest.TestCase):
    def setUp(self):
        self.banco = Banco()
        self.banco.registrar_relatorio("d1", "PC", "windows", 100.0, {})

    def test_abre_atualiza_e_resolve(self):
        P = regras.Problema
        ev = regras.sincronizar(self.banco, "d1", [P("disco:C:", regras.ALERTA, "81%")], 200.0)
        self.assertEqual([e["tipo"] for e in ev], ["aberto"])
        # mesmo problema, detalhe novo: silencioso
        self.assertEqual(regras.sincronizar(self.banco, "d1", [P("disco:C:", regras.ALERTA, "82%")], 210.0), [])
        self.assertEqual(self.banco.abertos_de("d1")["disco:C:"]["detalhe"], "82%")
        # piorou: avisa de novo
        ev = regras.sincronizar(self.banco, "d1", [P("disco:C:", regras.CRITICO, "91%")], 220.0)
        self.assertEqual([e["tipo"] for e in ev], ["aberto"])
        # melhorou: troca em silêncio
        self.assertEqual(regras.sincronizar(self.banco, "d1", [P("disco:C:", regras.ALERTA, "85%")], 230.0), [])
        self.assertEqual(self.banco.abertos_de("d1")["disco:C:"]["gravidade"], regras.ALERTA)
        # sumiu: resolvido
        ev = regras.sincronizar(self.banco, "d1", [], 240.0)
        self.assertEqual([e["tipo"] for e in ev], ["resolvido"])
        self.assertEqual(self.banco.abertos_de("d1"), {})

    def test_escopo_nao_resolve_o_que_nao_sabe(self):
        P = regras.Problema
        regras.sincronizar(self.banco, "d1", [P("disco:C:", regras.ALERTA, "81%")], 200.0)
        ev = regras.sincronizar(self.banco, "d1", [regras.problema_offline("PC", 300)], 500.0, {"offline": 150.0}, {"offline"})
        self.assertEqual([(e["tipo"], e["codigo"]) for e in ev], [("aberto", "offline")],
                         "ao sumir, o disco cheio não pode virar 'resolvido'")
        self.assertEqual(set(self.banco.abertos_de("d1")), {"disco:C:", "offline"})

    def test_abertura_personalizada_para_offline(self):
        regras.sincronizar(self.banco, "d1", [regras.problema_offline("PC", 300)], 500.0, {"offline": 150.0})
        self.assertEqual(self.banco.abertos_de("d1")["offline"]["aberto_em"], 150.0)


class TestBanco(unittest.TestCase):
    def test_sonda_falhas_seguidas_e_zera(self):
        b = Banco()
        id_ = b.adicionar_sonda("TV", "192.168.0.9", 1.0)
        self.assertEqual(b.adicionar_sonda("outra", "192.168.0.9", 2.0), id_, "mesmo host não duplica")
        self.assertEqual(b.registrar_sonda(id_, 10.0, None), 1)
        self.assertEqual(b.registrar_sonda(id_, 20.0, None), 2)
        self.assertEqual(b.registrar_sonda(id_, 30.0, 12.5), 0)
        self.assertEqual(b.dispositivo(id_)["latencia_ms"], 12.5)

    def test_historico_limpeza_e_cascata(self):
        b = Banco()
        b.registrar_relatorio("d1", "PC", "linux", 1.0, {"x": 1})
        for t in (10, 20, 30):
            b.registrar_metrica("d1", t, cpu=t)
        self.assertEqual([m["cpu"] for m in b.historico("d1", 15)], [20, 30])
        b.limpar_antigos(25)
        self.assertEqual(len(b.historico("d1", 0)), 1)
        b.abrir_problema("d1", "ram", "ALERTA", "x", 5.0)
        self.assertTrue(b.remover("d1"))
        self.assertEqual(b.problemas(), [], "apagar o dispositivo apaga os problemas dele")

    def test_acoes(self):
        b = Banco()
        b.registrar_relatorio("d1", "PC", "linux", 1.0, {})
        a = b.iniciar_acao("d1", "atualizar_apps", 5.0)
        self.assertTrue(b.acao_em_andamento("d1"))
        b.finalizar_acao(a, 9.0, 0, "ok")
        self.assertFalse(b.acao_em_andamento("d1"))
        self.assertEqual(b.acoes("d1")[0]["codigo"], 0)


class TestSondas(unittest.TestCase):
    def test_host_valido(self):
        for ok in ("192.168.10.5", "tv-sala.local", "pc_01"):
            self.assertTrue(sondas.host_valido(ok), ok)
        for ruim in ("", "-oProxyCommand=x", "a b", "a;b", "$(x)", "x" * 300):
            self.assertFalse(sondas.host_valido(ruim), ruim)

    def test_latencia(self):
        self.assertEqual(sondas.parse_latencia_ping("64 bytes from x: icmp_seq=1 ttl=64 time=12.3 ms"), 12.3)
        self.assertEqual(sondas.parse_latencia_ping("Resposta de x: bytes=32 tempo=7ms TTL=64"), 7.0)
        self.assertEqual(sondas.parse_latencia_ping("Resposta de x: bytes=32 tempo<1ms TTL=64"), 0.5)
        self.assertIsNone(sondas.parse_latencia_ping("Request timed out"))

    def test_ping_com_executor_falso(self):
        ok = lambda cmd, **kw: SimpleNamespace(returncode=0, stdout="time=3.5 ms", stderr="")
        falha = lambda cmd, **kw: SimpleNamespace(returncode=1, stdout="", stderr="")
        self.assertEqual(sondas.ping("10.0.0.1", executar=ok), 3.5)
        self.assertIsNone(sondas.ping("10.0.0.1", executar=falha))
        self.assertIsNone(sondas.ping("-oProxyCommand=x", executar=ok), "host malicioso nem chega ao ping")

        def sem_ping(cmd, **kw):
            raise FileNotFoundError

        self.assertIsNone(sondas.ping("10.0.0.1", executar=sem_ping))

    def test_tcp(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0)); s.listen(1)
            porta = s.getsockname()[1]
            self.assertIsNotNone(sondas.tcp("127.0.0.1", porta))
        self.assertIsNone(sondas.tcp("127.0.0.1", porta))   # fechou
        self.assertIsNone(sondas.tcp("127.0.0.1", 0))


class TestAlertas(unittest.TestCase):
    EV = {"tipo": "aberto", "gravidade": "CRÍTICO", "dispositivo": "PC-A", "detalhe": "Disco C:\\ em 93%"}

    def test_formatar(self):
        self.assertIn("CRÍTICO em PC-A", alertas.formatar(self.EV))
        self.assertTrue(alertas.formatar({**self.EV, "tipo": "resolvido"}).startswith("✅ Resolvido"))

    def test_canais_e_isolamento_de_falha(self):
        enviados = []

        def post(url, corpo):
            enviados.append(url)
            if "webhook" in url:
                raise OSError("fora do ar")

        email = mock.Mock()
        n = alertas.Notificador({"telegram": {"token": "T", "chat_id": "1"}, "webhook": {"url": "http://x/webhook"},
                                 "email": {"servidor": "s", "de": "a", "para": "b"}}, post=post, email=email, log=lambda *_: None)
        falhas = n.notificar(self.EV)
        self.assertEqual(falhas, ["webhook"])
        self.assertTrue(email.called, "um canal com defeito não impede os outros")
        self.assertEqual(len(enviados), 2)

    def test_sem_config_nao_faz_nada(self):
        n = alertas.Notificador({})
        self.assertFalse(n.ativo)
        self.assertEqual(n.notificar(self.EV), [])


class TestSsh(unittest.TestCase):
    CFG = {"host": "192.168.10.20", "usuario": "gabriel", "porta": 22, "chave": "~/.ssh/id_ed25519"}

    def test_validacao(self):
        self.assertEqual(ssh.validar_config(self.CFG)["usuario"], "gabriel")
        for ruim in ({"host": "-oProxyCommand=x"}, {"usuario": "-oProxyCommand=x"}, {"usuario": "a b"},
                     {"usuario": "a;rm"}, {"porta": 0}, {"porta": "22"}, {"chave": "-oFoo"}):
            with self.assertRaises(ssh.ConfigSshInvalida, msg=str(ruim)):
                ssh.validar_config({**self.CFG, **ruim})

    def test_comando_montado(self):
        cmd = ssh.montar_ssh(self.CFG, "echo oi")
        self.assertEqual(cmd[0], "ssh")
        self.assertIn("BatchMode=yes", cmd)
        self.assertEqual(cmd[-2], "gabriel@192.168.10.20")
        self.assertEqual(cmd[-1], "echo oi")
        self.assertNotIn("-tt", cmd)

    def test_so_acoes_fixas(self):
        self.assertIn("apt-get", ssh.comando_remoto("linux", "atualizar_apps"))
        self.assertIn("winget", ssh.comando_remoto("windows", "atualizar_apps"))
        for so, acao in (("linux", "rm -rf /"), ("macos", "atualizar_apps"), ("windows", "reiniciar")):
            with self.assertRaises(ssh.ConfigSshInvalida):
                ssh.comando_remoto(so, acao)

    def test_executar(self):
        ok = lambda cmd, **kw: SimpleNamespace(returncode=0, stdout="feito", stderr="")
        self.assertEqual(ssh.executar(self.CFG, "linux", "atualizar_apps", rodar=ok), {"codigo": 0, "saida": "feito"})

        def estoura(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 1)

        def sem_ssh(cmd, **kw):
            raise FileNotFoundError

        self.assertEqual(ssh.executar(self.CFG, "linux", "atualizar_apps", rodar=estoura)["codigo"], -1)
        self.assertIn("não está instalado", ssh.executar(self.CFG, "linux", "atualizar_apps", rodar=sem_ssh)["saida"])
        longo = lambda cmd, **kw: SimpleNamespace(returncode=0, stdout="x" * 10000, stderr="")
        self.assertEqual(len(ssh.executar(self.CFG, "linux", "atualizar_apps", rodar=longo)["saida"]), ssh.LIMITE_SAIDA)

    def test_config_em_arquivo(self):
        with tempfile.TemporaryDirectory() as tmp:
            arq = Path(tmp) / "ssh.json"
            ssh.salvar_config(arq, "d1", self.CFG)
            self.assertEqual(ssh.carregar_config(arq)["d1"]["host"], "192.168.10.20")
            arq.write_text(json.dumps({"ruim": {"host": "-x", "usuario": "u"}, "d1": self.CFG}))
            self.assertEqual(list(ssh.carregar_config(arq)), ["d1"], "entrada inválida é ignorada")
            arq.write_text("não é json", encoding="utf-8")
            self.assertEqual(ssh.carregar_config(arq), {})
            arq.write_bytes("{\"d1\": \"não\"}".encode("cp1252"))      # salvo em ANSI (Bloco de Notas antigo)
            self.assertEqual(ssh.carregar_config(arq), {}, "arquivo em outra codificação não pode derrubar o servidor")


class TestLimparRelatorio(unittest.TestCase):
    def test_remove_lixo_e_limita_tamanho(self):
        bruto = relatorio()
        bruto.update({"hostname": "x" * 1000, "cpu": 500, "extra_perigoso": "<script>",
                      "ram": {"percentual": "alto"}, "discos": "nao-e-lista", "rede": "texto"})
        bruto["aplicativos"]["instalados"] = [{"nome": "a" * 999, "versao": "1"}] * 2000
        r = servidor.limpar_relatorio(bruto)
        self.assertEqual(len(r["hostname"]), 60)
        self.assertIsNone(r["cpu"])
        self.assertIsNone(r["ram"]["percentual"])
        self.assertEqual(r["discos"], [])
        self.assertNotIn("extra_perigoso", r)
        self.assertEqual(len(r["aplicativos"]["instalados"]), 500)
        self.assertEqual(len(r["aplicativos"]["instalados"][0]["nome"]), 120)
        self.assertEqual(r["rede"]["ip"], None)

    def test_vazio(self):
        r = servidor.limpar_relatorio({})
        self.assertIsNone(r["cpu"]); self.assertEqual(r["discos"], [])


def criar_app(**kw):
    relogio = Relogio()
    app = servidor.App(Banco(), "token-agentes", agora=relogio, log=lambda *_: None,
                       testar=kw.pop("testar", lambda h, p: None), **kw)
    return app, relogio


class TestApp(unittest.TestCase):
    def test_fluxo_offline_e_volta(self):
        app, rel = criar_app()
        app.processar_relatorio(relatorio())
        rel.avancar(60); app.verificar()
        self.assertEqual(app.visao()[0]["status"], "ok")
        rel.avancar(600); app.verificar()
        d = app.visao()[0]
        self.assertEqual(d["status"], "offline")
        self.assertGreaterEqual(d["offline_ha_s"], 600, "conta desde o último contato, não desde a detecção")
        self.assertIsNone(d["ligado_ha_s"])
        app.processar_relatorio(relatorio())   # voltou
        self.assertEqual(app.visao()[0]["status"], "ok")

    def test_problemas_sobrevivem_ao_ficar_offline_sem_avisos_falsos(self):
        notif = mock.Mock(); notif.ativo = True
        app, rel = criar_app()
        app.notificador = notif
        app.processar_relatorio(relatorio(discos=[("C:\\", 95)]))
        rel.avancar(600); app.verificar()                       # PC desligou
        d = app.visao()[0]
        self.assertEqual(d["status"], "offline")
        self.assertIn("disco:C:\\", {p["codigo"] for p in d["problemas"]}, "o disco cheio continua registrado")
        app.processar_relatorio(relatorio(discos=[("C:\\", 95)]))   # voltou, disco ainda cheio
        eventos = [(e["tipo"], e["codigo"]) for c in notif.notificar_em_segundo_plano.call_args_list for e in c.args[0]]
        self.assertEqual(eventos, [("aberto", "disco:C:\\"), ("aberto", "offline"), ("resolvido", "offline")],
                         "sem 'resolvido' falso do disco e sem avisar o mesmo disco duas vezes")

    def test_disponibilidade(self):
        app, rel = criar_app()
        app.processar_relatorio(relatorio())
        rel.avancar(3600); app.processar_relatorio(relatorio())          # 1h ligado
        rel.avancar(1800); app.verificar()                               # 30 min sem contato -> offline
        rel.avancar(1800); app.processar_relatorio(relatorio())          # volta; ficou fora ~1h
        # janela de 7200s; ficou fora de t=3600 (último contato) até t=7200: metade do tempo
        self.assertEqual(app.visao()[0]["disponibilidade_24h"], 50.0)

    def test_ram_so_alerta_depois_de_3_leituras(self):
        app, rel = criar_app()
        for _ in range(2):
            app.processar_relatorio(relatorio(ram=97)); rel.avancar(60)
        self.assertEqual(app.visao()[0]["status"], "ok")
        app.processar_relatorio(relatorio(ram=97))
        self.assertEqual(app.visao()[0]["status"], "critico")
        app.processar_relatorio(relatorio(ram=30))
        self.assertEqual(app.visao()[0]["status"], "ok")

    def test_notifica_so_nas_mudancas(self):
        notif = mock.Mock(); notif.ativo = True
        app, rel = criar_app()
        app.notificador = notif
        app.processar_relatorio(relatorio(discos=[("C:\\", 95)]))
        app.processar_relatorio(relatorio(discos=[("C:\\", 96)]))     # mesmo problema: não repete
        app.processar_relatorio(relatorio(discos=[("C:\\", 40)]))     # resolvido
        tipos = [e["tipo"] for chamada in notif.notificar_em_segundo_plano.call_args_list for e in chamada.args[0]]
        self.assertEqual(tipos, ["aberto", "resolvido"])

    def test_sonda_so_cai_apos_3_falhas(self):
        resp = [None]
        app, rel = criar_app(testar=lambda h, p: resp[0])
        app.banco.adicionar_sonda("iPhone", "192.168.10.60", rel())
        for esperado in ("ok", "ok", "offline"):
            rel.avancar(30); app.verificar()
            estado = app.visao()[0]["status"]
            self.assertEqual(estado if esperado == "offline" else "ok", esperado)
        resp[0] = 8.0
        rel.avancar(30); app.verificar()
        d = app.visao()[0]
        self.assertEqual(d["status"], "ok")
        self.assertEqual(d["latencia_ms"], 8.0)

    def test_id_invalido(self):
        app, _ = criar_app()
        for ruim in (None, "", "curto", "a b c d e f g h", "x" * 100, 123):
            with self.assertRaises(ValueError):
                app.processar_relatorio({"id": ruim})


class TestHttp(unittest.TestCase):
    def setUp(self):
        self.chamadas_ssh = []

        def falso_ssh(cfg, so, acao):
            self.chamadas_ssh.append((cfg["host"], so, acao))
            return {"codigo": 0, "saida": "atualizado"}

        self.relogio = Relogio()
        self.app = servidor.App(Banco(), "tok-agente", agora=self.relogio, log=lambda *_: None, admin_token="tok-admin",
                                ssh_config={"abcdef1234567890": {"host": "192.168.10.20", "usuario": "g", "porta": 22, "chave": None}},
                                rodar_ssh=falso_ssh, em_segundo_plano=False)
        self.srv = servidor.criar_servidor(self.app, "127.0.0.1", 0)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        self.srv.shutdown(); self.srv.server_close()

    def req(self, caminho, corpo=None, token=None):
        r = urllib.request.Request(self.base + caminho, method="POST" if corpo is not None else "GET",
                                   data=json.dumps(corpo).encode() if corpo is not None else None,
                                   headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})})
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, json.loads(resp.read() or b"{}") if "json" in resp.headers.get("Content-Type", "") else resp.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"{}")

    def test_relatorio_exige_token(self):
        self.assertEqual(self.req("/api/relatorio", relatorio())[0], 401)
        self.assertEqual(self.req("/api/relatorio", relatorio(), "errado")[0], 401)
        self.assertEqual(self.req("/api/relatorio", relatorio(), "tok-agente")[0], 200)

    def test_erro_com_corpo_grande_ainda_chega_ao_cliente(self):
        """Regressão (Windows): 401 sem ler o corpo fazia o SO resetar a conexão."""
        grande = {**relatorio(), "lixo": "x" * 400_000}
        self.assertEqual(self.req("/api/relatorio", grande, "errado")[0], 401)
        self.assertEqual(self.req("/api/dispositivos/abcdef1234567890/acao", grande, "errado")[0], 401)
        self.assertEqual(self.req("/api/outra-coisa", grande, "tok-agente")[0], 404)

    def test_token_de_admin_nao_serve_para_relatorio_e_vice_versa(self):
        self.assertEqual(self.req("/api/relatorio", relatorio(), "tok-admin")[0], 401)
        self.req("/api/relatorio", relatorio(), "tok-agente")
        corpo = {"acao": "atualizar_apps"}
        self.assertEqual(self.req("/api/dispositivos/abcdef1234567890/acao", corpo, "tok-agente")[0], 401,
                         "um PC comprometido (que tem o token do agente) não pode mandar atualizar outros")

    def test_json_invalido_e_id_ruim(self):
        self.assertEqual(self.req("/api/relatorio", {**relatorio(), "id": "x"}, "tok-agente")[0], 400)
        r = urllib.request.Request(self.base + "/api/relatorio", data=b"{nao json", method="POST",
                                   headers={"Authorization": "Bearer tok-agente", "Content-Length": "9"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(r)
        self.assertEqual(ctx.exception.code, 400)

    def test_painel_e_api_de_leitura(self):
        self.req("/api/relatorio", relatorio(cpu=12), "tok-agente")
        status, html = self.req("/")
        self.assertEqual(status, 200); self.assertIn(b"NetPulse", html)
        status, lista = self.req("/api/dispositivos")
        self.assertEqual(lista[0]["nome"], "PC-A")
        self.assertTrue(lista[0]["ssh"])
        status, hist = self.req("/api/dispositivos/abcdef1234567890/historico?horas=1")
        self.assertEqual(len(hist), 1)
        self.assertEqual(self.req("/nao-existe")[0], 404)

    def test_acao_ssh_fim_a_fim(self):
        self.req("/api/relatorio", relatorio(), "tok-agente")
        status, _ = self.req("/api/dispositivos/abcdef1234567890/acao", {"acao": "atualizar_apps"}, "tok-admin")
        self.assertEqual(status, 200)
        self.assertEqual(self.chamadas_ssh, [("192.168.10.20", "windows", "atualizar_apps")])
        self.assertEqual(self.req("/api/acoes")[1][0]["codigo"], 0)

    def test_acao_invalida_ou_sem_ssh(self):
        self.req("/api/relatorio", relatorio(), "tok-agente")
        self.assertEqual(self.req("/api/dispositivos/abcdef1234567890/acao", {"acao": "rm -rf /"}, "tok-admin")[0], 400)
        self.assertEqual(self.req("/api/dispositivos/abcdef1234567890/acao", {}, "tok-admin")[0], 400)
        self.assertEqual(self.req("/api/dispositivos/naoexiste123/acao", {"acao": "atualizar_apps"}, "tok-admin")[0], 400)
        self.assertEqual(self.chamadas_ssh, [])
        self.app.admin_token = None   # ações remotas desligadas
        self.assertEqual(self.req("/api/dispositivos/abcdef1234567890/acao", {"acao": "atualizar_apps"}, None)[0], 401)
        self.assertEqual(self.req("/api/dispositivos/abcdef1234567890/acao", {"acao": "atualizar_apps"}, "None")[0], 401,
                         "com as ações desligadas, nem o texto 'None' pode servir de token")
        self.assertEqual(self.chamadas_ssh, [])

    def test_nao_aceita_duas_acoes_ao_mesmo_tempo(self):
        self.req("/api/relatorio", relatorio(), "tok-agente")
        self.app.banco.iniciar_acao("abcdef1234567890", "atualizar_apps", 1.0)    # uma já em andamento
        status, resp = self.req("/api/dispositivos/abcdef1234567890/acao", {"acao": "atualizar_apps"}, "tok-admin")
        self.assertEqual(status, 400); self.assertIn("em andamento", resp["erro"])


class TestCsvECli(unittest.TestCase):
    def test_ler_hosts(self):
        hosts, avisos = parsers_csv.ler_hosts("\ufeffip;mac;hostname;descricao\n10.0.0.1;aa;fw;Firewall\n10.0.0.2;;;Switch\n-x;;;ruim\n")
        self.assertEqual(hosts, [("10.0.0.1", "fw"), ("10.0.0.2", "Switch")])
        self.assertEqual(len(avisos), 1)
        self.assertEqual(parsers_csv.ler_hosts("a,b\n1,2")[1], ["o CSV precisa ter uma coluna 'ip'"])
        self.assertEqual(parsers_csv.ler_hosts("ip,nome\n10.0.0.9,tv")[0], [("10.0.0.9", "tv")])

    def test_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            dados = ["--dados", tmp]
            self.assertEqual(cli.main(dados + ["adicionar", "--nome", "TV", "--host", "10.0.0.5"]), 0)
            self.assertEqual(cli.main(dados + ["adicionar", "--nome", "X", "--host=-oProxyCommand=a"]), 1)
            csv = Path(tmp) / "i.csv"
            csv.write_text("ip;mac;hostname;descricao\n10.0.0.1;;fw;Firewall\n", encoding="utf-8")
            ansi = Path(tmp) / "ansi.csv"
            ansi.write_bytes("ip;hostname\n10.0.0.7;impressora-sala-1ª\n".encode("cp1252"))   # Excel/Bloco de Notas em ANSI
            self.assertEqual(cli.main(dados + ["importar", str(ansi)]), 0)
            self.assertEqual(cli.main(dados + ["importar", str(csv)]), 0)
            self.assertEqual(cli.main(dados + ["importar", str(Path(tmp) / "nao.csv")]), 1)
            with Banco(Path(tmp) / "netpulse.db") as banco:
                self.assertEqual(len(banco.dispositivos()), 3)
                id_ = banco.dispositivos()[0]["id"]
            self.assertEqual(cli.main(dados + ["ssh-configurar", id_, "--host", "10.0.0.1", "--usuario", "g"]), 1,
                             "sonda não tem agente: SSH só vale para computadores com agente")
            with Banco(Path(tmp) / "netpulse.db") as banco:
                banco.registrar_relatorio("abcdef1234567890", "PC", "linux", 1.0, {})
            self.assertEqual(cli.main(dados + ["ssh-configurar", "abcdef1234567890", "--host", "10.0.0.8", "--usuario", "g"]), 0)
            self.assertEqual(cli.main(dados + ["ssh-configurar", "abcdef1234567890", "--host=-x", "--usuario", "g"]), 1)
            self.assertEqual(cli.main(dados + ["ssh-configurar", "naoexiste", "--host", "10.0.0.8", "--usuario", "g"]), 1)
            self.assertEqual(cli.main(dados + ["remover", "naoexiste"]), 1)
            self.assertEqual(cli.main(dados + ["agente", "--servidor", "http://x"]), 1, "sem token")


class TestAgente(unittest.TestCase):
    def test_id_estavel(self):
        from netpulse import agente
        with tempfile.TemporaryDirectory() as tmp:
            arq = Path(tmp) / "sub" / "id"
            self.assertEqual(agente.carregar_id(arq), agente.carregar_id(arq))

    def test_envio_para_servidor_real(self):
        from netpulse import agente
        app = servidor.App(Banco(), "tok", log=lambda *_: None)
        srv = servidor.criar_servidor(app, "127.0.0.1", 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{srv.server_address[1]}"
            rel = agente.montar_relatorio("abcdef1234567890", amostra_cpu_s=0.05,
                                          cache=coletores.CacheAtualizacoes(buscar=lambda: {"pendentes": 0, "reiniciar": False}),
                                          cache_apps=coletores.CacheAtualizacoes(buscar=lambda: {"total": 0, "instalados": [], "atualizaveis": []}))
            self.assertEqual(agente.enviar(url, "tok", rel)[0], True)
            ok, msg = agente.enviar(url, "errado", rel)
            self.assertFalse(ok); self.assertIn("token", msg)
            self.assertEqual(len(app.banco.dispositivos()), 1)
        finally:
            srv.shutdown(); srv.server_close()
        ok, msg = agente.enviar("http://127.0.0.1:1", "tok", rel, timeout=1)
        self.assertFalse(ok); self.assertIn("sem conexão", msg)


if __name__ == "__main__":
    unittest.main()
