"""Regras: transformam o último relatório de um dispositivo em uma lista de problemas."""

from __future__ import annotations

from dataclasses import dataclass, field

CRITICO, ALERTA, INFO = "CRÍTICO", "ALERTA", "INFO"
PESO = {INFO: 1, ALERTA: 2, CRITICO: 3}

LIMITES_PADRAO = {
    "offline_apos_s": 180,      # sem contato do agente por mais que isso = offline
    "sonda_falhas": 3,          # testes de ping/porta seguidos que precisam falhar
    "ram_alerta": 85.0, "ram_critico": 95.0,
    "cpu_alerta": 95.0,
    "disco_alerta": 80.0, "disco_critico": 90.0,
    "atualizacoes_alerta": 10,  # a partir de quantas pendentes vira ALERTA
    "apps_alerta": 10,          # idem para aplicativos desatualizados
    "confirmacoes_ram": 3,      # leituras seguidas acima do limite antes de avisar
    "confirmacoes_cpu": 5,
}


@dataclass(frozen=True)
class Problema:
    codigo: str
    gravidade: str
    detalhe: str
    confirmar: int = field(default=1, compare=False)


def _gb(bytes_: int) -> str:
    return f"{bytes_ / 1024 ** 3:.1f} GB"


def avaliar_relatorio(relatorio: dict, limites: dict | None = None) -> list[Problema]:
    """Problemas de um relatório de agente (sem considerar se o agente está offline)."""
    lim = {**LIMITES_PADRAO, **(limites or {})}
    problemas: list[Problema] = []

    ram = (relatorio.get("ram") or {}).get("percentual")
    if ram is not None:
        if ram >= lim["ram_critico"]:
            problemas.append(Problema("ram", CRITICO, f"Memória em {ram:.0f}%", lim["confirmacoes_ram"]))
        elif ram >= lim["ram_alerta"]:
            problemas.append(Problema("ram", ALERTA, f"Memória em {ram:.0f}%", lim["confirmacoes_ram"]))

    cpu = relatorio.get("cpu")
    if cpu is not None and cpu >= lim["cpu_alerta"]:
        problemas.append(Problema("cpu", ALERTA, f"CPU em {cpu:.0f}% de forma contínua", lim["confirmacoes_cpu"]))

    for disco in relatorio.get("discos") or []:
        pct = disco.get("percentual")
        if pct is None:
            continue
        gravidade = CRITICO if pct >= lim["disco_critico"] else ALERTA if pct >= lim["disco_alerta"] else None
        if gravidade:
            problemas.append(Problema(
                f"disco:{disco['ponto']}", gravidade,
                f"Disco {disco['ponto']} em {pct:.0f}% ({_gb(disco['livre'])} livres)"))

    atu = relatorio.get("atualizacoes") or {}
    pendentes = atu.get("pendentes")
    if pendentes:
        gravidade = ALERTA if pendentes >= lim["atualizacoes_alerta"] else INFO
        problemas.append(Problema("atualizacoes", gravidade, f"{pendentes} atualização(ões) pendente(s)"))
    apps = (relatorio.get("aplicativos") or {}).get("atualizaveis")
    if apps:
        nomes = ", ".join(a["nome"] for a in apps[:3]) + ("..." if len(apps) > 3 else "")
        gravidade = ALERTA if len(apps) >= lim["apps_alerta"] else INFO
        problemas.append(Problema("apps", gravidade, f"{len(apps)} aplicativo(s) com atualização: {nomes}"))
    if atu.get("reiniciar"):
        problemas.append(Problema("reiniciar", INFO, "Reinicialização pendente para concluir atualizações"))
    return problemas


def problema_offline(nome: str, segundos: float | None) -> Problema:
    quando = "nunca respondeu" if segundos is None else f"sem contato há {int(segundos // 60)} min"
    return Problema("offline", CRITICO, f"{nome} {quando}")


def status_de(problemas) -> str:
    """'offline', 'critico', 'alerta', 'info' ou 'ok', pelo pior problema aberto."""
    codigos = {getattr(p, "codigo", None) or p["codigo"] for p in problemas}
    if "offline" in codigos:
        return "offline"
    pior = 0
    for p in problemas:
        gravidade = getattr(p, "gravidade", None) or p["gravidade"]
        pior = max(pior, PESO[gravidade])
    return {0: "ok", 1: "info", 2: "alerta", 3: "critico"}[pior]


class Avaliador:
    """Confirma problemas só depois de N leituras seguidas (evita alarme por pico rápido)."""

    def __init__(self):
        self._contagem: dict[tuple[str, str], int] = {}

    def confirmar(self, dispositivo_id: str, brutos: list[Problema]) -> list[Problema]:
        presentes = {p.codigo for p in brutos}
        for chave in [k for k in self._contagem if k[0] == dispositivo_id and k[1] not in presentes]:
            del self._contagem[chave]
        confirmados = []
        for p in brutos:
            chave = (dispositivo_id, p.codigo)
            self._contagem[chave] = self._contagem.get(chave, 0) + 1
            if self._contagem[chave] >= p.confirmar:
                confirmados.append(p)
        return confirmados


def sincronizar(banco, dispositivo_id: str, confirmados: list[Problema], agora: float,
                abertura: dict | None = None, escopo: set[str] | None = None) -> list[dict]:
    """Abre/fecha problemas no banco e devolve os eventos que merecem aviso.

    Evento 'aberto' só quando o problema é novo ou piorou de gravidade;
    'resolvido' quando some. Mudança de detalhe (91% → 92%) é gravada em silêncio.

    ``escopo`` limita quais códigos esta chamada pode abrir/fechar. Usado quando o dispositivo
    some: só 'offline' é atualizado, e os outros problemas ficam como estavam (não dá para
    saber se foram resolvidos sem um relatório novo).
    """
    eventos = []
    abertura = abertura or {}  # ex.: {'offline': ts do último contato}, para a duração sair certa
    atuais = banco.abertos_de(dispositivo_id)
    if escopo is not None:
        atuais = {c: v for c, v in atuais.items() if c in escopo}
        confirmados = [p for p in confirmados if p.codigo in escopo]
    for p in confirmados:
        antigo = atuais.get(p.codigo)
        if antigo is None:
            banco.abrir_problema(dispositivo_id, p.codigo, p.gravidade, p.detalhe, abertura.get(p.codigo, agora))
            eventos.append({"tipo": "aberto", "codigo": p.codigo, "gravidade": p.gravidade, "detalhe": p.detalhe})
        elif antigo["gravidade"] != p.gravidade:
            banco.fechar_problema(antigo["id"], agora)
            banco.abrir_problema(dispositivo_id, p.codigo, p.gravidade, p.detalhe, agora)
            if PESO[p.gravidade] > PESO[antigo["gravidade"]]:
                eventos.append({"tipo": "aberto", "codigo": p.codigo, "gravidade": p.gravidade, "detalhe": p.detalhe})
        elif antigo["detalhe"] != p.detalhe:
            banco.atualizar_detalhe(antigo["id"], p.detalhe)
    codigos = {p.codigo for p in confirmados}
    for codigo, antigo in atuais.items():
        if codigo not in codigos:
            banco.fechar_problema(antigo["id"], agora)
            eventos.append({"tipo": "resolvido", "codigo": codigo, "gravidade": antigo["gravidade"],
                            "detalhe": antigo["detalhe"]})
    return eventos
