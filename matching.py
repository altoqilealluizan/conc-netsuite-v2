"""
matching.py
===========
Motor de "waterfall matching" (cascata de prioridade) para conciliação
linha-a-linha entre Adquirente e Pagamentos/Recebimentos, com classificação
de confiança — especificação de negócio fornecida pela usuária.

MAPEAMENTO DE CAMPOS VALIDADO CONTRA A BASE REAL (jul/2026)
------------------------------------------------------------------------
Conceito           | Adquirente             | Pagamentos (CustPymt)      | Recebimentos (Journal)
-------------------|------------------------|-----------------------------|------------------------
TID/NSU/ARP        | memo (regex)           | custbody_nscs_tid/nsu/arp   | memo (regex)
Fatura             | memo (regex) > custcol | custbody_nscs_faturavindi   | custcolcustcol_id_fatura
                   | _id_fatura             |                             | > memo (regex)
Parcela            | memo (regex "parcela N")   — mesma extração nas 3 origens
ID da Transação    | ADQ×PAG: custcolcustcol_n_pagamento (linha ADQ) == tranid do CustPymt
                   | ADQ×REC: custcolcustcoldata_idsaque (linha ADQ) == idsaque (linha REC)
Valor              | ADQ×PAG: débito ADQ vs débito PAG · ADQ×REC: crédito ADQ vs débito REC
Data               | trandate
Memo               | texto livre da linha (fallback de busca textual e similaridade)

⚠️ DECISÃO DE MAPEAMENTO A REVISAR COM O NEGÓCIO: o memo da Adquirente e de
Pagamentos também carrega um campo "Transação NNNNNN" (aparentemente um ID
interno do gateway Vindi) — testamos e ele NÃO aparece de forma consistente
nos dois lados para a mesma venda (ver validação na sessão de implementação),
por isso NÃO foi usado como "ID da Transação". Usamos os campos NetSuite
custcolcustcol_n_pagamento / custcolcustcoldata_idsaque, que validamos como
cross-reference direto e consistente. Se "ID da Transação" para o negócio
significar outro campo, ajustar aqui.

⚠️ "ID da Transação" em ADQ×REC é N:1 (várias vendas formam 1 saque/depósito)
— tratado corretamente pelo motor (permite N adq -> 1 ctp só para essa regra).
Os demais campos são tratados como 1:1 (cada linha é usada no máximo 1 vez).

CONFIANÇA: os pontos fixos abaixo (100/95/90/80/60%) replicam exatamente a
tabela de classificação informada. Para as combinações que não tinham um
percentual explícito na tabela (a maior parte dos Níveis 3, 5, 6 e 7), foi
feita uma interpolação — documentada rule a rule abaixo — sinalizada na tela
do app para revisão do time.
"""

from __future__ import annotations

import difflib
import hashlib
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

# ---------------------------------------------------------------------------
# Código de documento da conciliação — ESTÁVEL (hash determinístico do par).
# Sempre gera o MESMO código para o mesmo par de lançamentos, não importa
# quando/quantas vezes o matching for rodado novamente — decisão validada
# com a usuária (2026-07-16). Formato: CONC-{ESCOPO}-{8 chars hex}.
# ---------------------------------------------------------------------------

def gerar_codigo_conciliacao(id_adq: str, id_ctp: str, escopo: str) -> str:
    """Gera o número de documento da conciliação: um hash SHA-256 (8 chars)
    derivado dos IDs internos das duas linhas casadas + o escopo. Estável:
    o mesmo par de lançamentos sempre produz o mesmo código."""
    base = f"{escopo}|{id_adq}|{id_ctp}"
    h = hashlib.sha256(base.encode("utf-8")).hexdigest()[:8].upper()
    return f"CONC-{escopo}-{h}"


# ---------------------------------------------------------------------------
# Campos usados na normalização (mesmo nome nos dois lados do pareamento)
# ---------------------------------------------------------------------------
CAMPOS_IDENTIFICADORES = ["tid", "nsu", "arp", "fatura", "id_transacao"]


@dataclass(frozen=True)
class RegraMatch:
    nivel: int
    nome: str
    campos: tuple  # colunas exigidas (igualdade exata após normalização)
    confianca: int
    permite_muitos_para_um: bool = False  # ex.: id_transacao em ADQ×REC (saque)


# ---------------------------------------------------------------------------
# Cascata de regras — EXATAMENTE na ordem de prioridade especificada.
# ---------------------------------------------------------------------------
REGRAS: list[RegraMatch] = [
    # Nível 1 — Match exato (100% — conforme tabela de confiança)
    RegraMatch(1, "ID da Transação", ("id_transacao",), 100, permite_muitos_para_um=True),
    RegraMatch(1, "TID", ("tid",), 100),
    RegraMatch(1, "NSU", ("nsu",), 100),
    RegraMatch(1, "ARP", ("arp",), 100),
    RegraMatch(1, "Fatura", ("fatura",), 100),

    # Nível 2 — Match exato + Valor (95% — conforme tabela)
    RegraMatch(2, "TID + Valor", ("tid", "valor"), 95),
    RegraMatch(2, "NSU + Valor", ("nsu", "valor"), 95),
    RegraMatch(2, "ARP + Valor", ("arp", "valor"), 95),
    RegraMatch(2, "Fatura + Valor", ("fatura", "valor"), 95),
    RegraMatch(2, "ID da Transação + Valor", ("id_transacao", "valor"), 95, permite_muitos_para_um=True),

    # Nível 3 — Match exato + Parcela (92% interpolado; Valor+Parcela=80% já
    # está explícito na tabela de confiança em "Média")
    RegraMatch(3, "TID + Parcela", ("tid", "parcela"), 92),
    RegraMatch(3, "NSU + Parcela", ("nsu", "parcela"), 92),
    RegraMatch(3, "ARP + Parcela", ("arp", "parcela"), 92),
    RegraMatch(3, "Fatura + Parcela", ("fatura", "parcela"), 92),
    RegraMatch(3, "Valor + Parcela", ("valor", "parcela"), 80),

    # Nível 4 — Match composto (90% — conforme tabela)
    RegraMatch(4, "TID + Valor + Parcela", ("tid", "valor", "parcela"), 90),
    RegraMatch(4, "NSU + Valor + Parcela", ("nsu", "valor", "parcela"), 90),
    RegraMatch(4, "ARP + Valor + Parcela", ("arp", "valor", "parcela"), 90),
    RegraMatch(4, "Fatura + Valor + Parcela", ("fatura", "valor", "parcela"), 90),
    RegraMatch(4, "ID da Transação + Valor + Parcela", ("id_transacao", "valor", "parcela"), 90, permite_muitos_para_um=True),

    # Nível 5 — Cruzamento entre identificadores (98/99/96% interpolados —
    # duas IDs batendo juntas é evidência muito forte, mesmo executando
    # depois do Nível 1-4 na ordem de prioridade)
    RegraMatch(5, "TID + NSU", ("tid", "nsu"), 98),
    RegraMatch(5, "TID + ARP", ("tid", "arp"), 98),
    RegraMatch(5, "NSU + ARP", ("nsu", "arp"), 98),
    RegraMatch(5, "TID + NSU + Valor", ("tid", "nsu", "valor"), 99),
    RegraMatch(5, "TID + ARP + Valor", ("tid", "arp", "valor"), 99),
    RegraMatch(5, "NSU + ARP + Valor", ("nsu", "arp", "valor"), 99),
    RegraMatch(5, "TID + NSU + Parcela", ("tid", "nsu", "parcela"), 96),
    RegraMatch(5, "TID + NSU + Valor + Parcela", ("tid", "nsu", "valor", "parcela"), 99),

    # Nível 6 — Cruzamento com Fatura (97-99% interpolados)
    RegraMatch(6, "Fatura + TID", ("fatura", "tid"), 99),
    RegraMatch(6, "Fatura + NSU", ("fatura", "nsu"), 99),
    RegraMatch(6, "Fatura + ARP", ("fatura", "arp"), 99),
    RegraMatch(6, "Fatura + TID + Valor", ("fatura", "tid", "valor"), 99),
    RegraMatch(6, "Fatura + NSU + Valor", ("fatura", "nsu", "valor"), 99),
    RegraMatch(6, "Fatura + ARP + Valor", ("fatura", "arp", "valor"), 99),

    # Nível 7 — Cruzamento com ID da Transação (99-100% interpolados)
    RegraMatch(7, "ID da Transação + TID", ("id_transacao", "tid"), 99, permite_muitos_para_um=True),
    RegraMatch(7, "ID da Transação + NSU", ("id_transacao", "nsu"), 99, permite_muitos_para_um=True),
    RegraMatch(7, "ID da Transação + ARP", ("id_transacao", "arp"), 99, permite_muitos_para_um=True),
    RegraMatch(7, "ID da Transação + TID + NSU + Valor", ("id_transacao", "tid", "nsu", "valor"), 100, permite_muitos_para_um=True),
]

# Nível 8 — Busca em texto (identificadores procurados dentro do memo da
# contraparte, na ordem de prioridade especificada). Confiança 80% (tabela:
# "Busca no Memo" = Média).
NIVEL_TEXTO = 8
CAMPOS_TEXTO_ORDEM = ["id_transacao", "fatura", "tid", "nsu", "arp"]
CONFIANCA_TEXTO = 80
TAMANHO_MIN_TOKEN_TEXTO = 5  # evita casar por trechos curtos/genéricos demais

# Nível 9 — Fallback aproximado
NIVEL_APROX = 9


# ---------------------------------------------------------------------------
# Preparação dos dados (normalização comum aos dois lados)
# ---------------------------------------------------------------------------

def preparar(df: pd.DataFrame, prefixo_id: str) -> pd.DataFrame:
    """Normaliza um DataFrame vindo do NetSuite para o esquema comum exigido
    pelo motor: colunas tid/nsu/arp/fatura/parcela/id_transacao (strings
    normalizadas ou None), valor (float > 0), data (datetime), memo (str)."""
    d = df.copy()
    if "_id" not in d.columns:
        d["_id"] = [f"{prefixo_id}-{i}" for i in range(len(d))]
    for col in ["tid", "nsu", "arp", "fatura", "parcela", "id_transacao"]:
        if col not in d.columns:
            d[col] = None
        d[col] = d[col].astype("string").str.strip().str.upper()
        d.loc[d[col].isin(["", "NAN", "NONE"]), col] = pd.NA
    d["valor"] = pd.to_numeric(d["valor"], errors="coerce").round(2)
    d = d[d["valor"].notna() & (d["valor"] > 0)].copy()
    d["data"] = pd.to_datetime(d["data"], errors="coerce")
    if "memo" not in d.columns:
        d["memo"] = ""
    d["memo"] = d["memo"].fillna("").astype(str)
    return d.reset_index(drop=True)


# ---------------------------------------------------------------------------
# Casamento exato (genérico) — 1 para 1, com desempate por menor diferença
# de valor e de data quando há mais de um candidato.
# ---------------------------------------------------------------------------

def _preparar_chaves_merge(df: pd.DataFrame, campos: list) -> tuple[pd.DataFrame, list]:
    """Substitui 'valor' (se presente em `campos`) por uma chave de merge
    derivada e arredondada, para que a coluna 'valor' original NÃO seja
    consumida pelo `on=` do merge — assim ela continua existindo depois do
    merge (com os sufixos _adq/_ctp) para o cálculo de diferença de valor."""
    d = df.copy()
    campos_efetivos = []
    for c in campos:
        if c == "valor":
            d["_valor_chave"] = d["valor"].round(2)
            campos_efetivos.append("_valor_chave")
        else:
            campos_efetivos.append(c)
    return d, campos_efetivos


def _casar_1_para_1(pend_adq: pd.DataFrame, pend_ctp: pd.DataFrame, campos: list) -> pd.DataFrame:
    a, campos_efetivos = _preparar_chaves_merge(pend_adq, campos)
    c, _ = _preparar_chaves_merge(pend_ctp, campos)
    a = a.dropna(subset=campos_efetivos)
    c = c.dropna(subset=campos_efetivos)
    if a.empty or c.empty:
        return pd.DataFrame()
    cand = a.merge(c, on=campos_efetivos, suffixes=("_adq", "_ctp"))
    if cand.empty:
        return cand
    cand["_dif_valor"] = (cand["valor_adq"] - cand["valor_ctp"]).abs()
    cand["_dif_dias"] = (cand["data_adq"] - cand["data_ctp"]).abs().dt.days.fillna(9999)
    cand = cand.sort_values(["_dif_valor", "_dif_dias"])
    usados_adq, usados_ctp, linhas = set(), set(), []
    for _, row in cand.iterrows():
        if row["_id_adq"] in usados_adq or row["_id_ctp"] in usados_ctp:
            continue
        usados_adq.add(row["_id_adq"])
        usados_ctp.add(row["_id_ctp"])
        linhas.append(row)
    return pd.DataFrame(linhas) if linhas else pd.DataFrame()


def _casar_muitos_para_um(pend_adq: pd.DataFrame, pend_ctp: pd.DataFrame, campos: list) -> pd.DataFrame:
    """N linhas do lado ADQ podem casar com a MESMA linha da contraparte
    (ex.: várias vendas formando 1 saque/depósito). Cada linha ADQ só é
    usada 1 vez; a linha da contraparte pode se repetir."""
    a, campos_efetivos = _preparar_chaves_merge(pend_adq, campos)
    c, _ = _preparar_chaves_merge(pend_ctp, campos)
    a = a.dropna(subset=campos_efetivos)
    c = c.dropna(subset=campos_efetivos)
    if a.empty or c.empty:
        return pd.DataFrame()
    cand = a.merge(c, on=campos_efetivos, suffixes=("_adq", "_ctp"))
    if cand.empty:
        return cand
    return cand.drop_duplicates(subset=["_id_adq"], keep="first")


# ---------------------------------------------------------------------------
# Nível 8 — Busca em texto
# ---------------------------------------------------------------------------

def _busca_texto(pend_adq: pd.DataFrame, pend_ctp: pd.DataFrame, limite_pares: int):
    if pend_adq.empty or pend_ctp.empty:
        return pd.DataFrame(), pend_adq, pend_ctp
    if len(pend_adq) * len(pend_ctp) > limite_pares:
        return pd.DataFrame(), pend_adq, pend_ctp  # volume grande — pulado (ver flag no app)

    memo_ctp_upper = pend_ctp["memo"].str.upper()
    usados_adq, usados_ctp = set(), set()
    linhas = []
    for campo in CAMPOS_TEXTO_ORDEM:
        if pend_adq.empty:
            break
        for _, ra in pend_adq.iterrows():
            if ra["_id"] in usados_adq:
                continue
            alvo = ra.get(campo)
            if pd.isna(alvo) or len(str(alvo)) < TAMANHO_MIN_TOKEN_TEXTO:
                continue
            disponiveis = ~pend_ctp["_id"].isin(usados_ctp)
            achou = pend_ctp[disponiveis & memo_ctp_upper.str.contains(str(alvo), regex=False, na=False)]
            if not achou.empty:
                rc = achou.iloc[0]
                linhas.append({
                    "_id_adq": ra["_id"], "_id_ctp": rc["_id"],
                    "valor_adq": ra["valor"], "valor_ctp": rc["valor"],
                    "data_adq": ra["data"], "data_ctp": rc["data"],
                    "numero_documento_adq": ra.get("numero_documento"),
                    "numero_documento_ctp": rc.get("numero_documento"),
                    "memo_adq": ra["memo"], "memo_ctp": rc["memo"],
                    "nivel": NIVEL_TEXTO, "regra": f"Busca em texto ({campo})",
                    "confianca": CONFIANCA_TEXTO,
                })
                usados_adq.add(ra["_id"])
                usados_ctp.add(rc["_id"])

    if not linhas:
        return pd.DataFrame(), pend_adq, pend_ctp
    matched = pd.DataFrame(linhas)
    pend_adq = pend_adq[~pend_adq["_id"].isin(matched["_id_adq"])]
    pend_ctp = pend_ctp[~pend_ctp["_id"].isin(matched["_id_ctp"])]
    return matched, pend_adq, pend_ctp


# ---------------------------------------------------------------------------
# Nível 9 — Fallback aproximado
# ---------------------------------------------------------------------------

def _cross(pend_adq: pd.DataFrame, pend_ctp: pd.DataFrame) -> pd.DataFrame:
    a = pend_adq.assign(_k=1)
    c = pend_ctp.assign(_k=1)
    return a.merge(c, on="_k", suffixes=("_adq", "_ctp")).drop(columns="_k")


def _fallback_aproximado(pend_adq: pd.DataFrame, pend_ctp: pd.DataFrame, limite_pares: int, limite_similaridade: int):
    pares_total = []

    def _tenta(nome: str, confianca: int, filtro_fn):
        nonlocal pend_adq, pend_ctp
        if pend_adq.empty or pend_ctp.empty or len(pend_adq) * len(pend_ctp) > limite_pares:
            return
        cand = _cross(pend_adq, pend_ctp)
        try:
            cand = cand[filtro_fn(cand)]
        except Exception:
            return
        if cand.empty:
            return
        cand = cand.copy()
        cand["_dif_valor"] = (cand["valor_adq"] - cand["valor_ctp"]).abs()
        cand["_dif_dias"] = (cand["data_adq"] - cand["data_ctp"]).abs().dt.days.fillna(9999)
        cand = cand.sort_values(["_dif_valor", "_dif_dias"])
        usados_adq, usados_ctp, linhas = set(), set(), []
        for _, row in cand.iterrows():
            if row["_id_adq"] in usados_adq or row["_id_ctp"] in usados_ctp:
                continue
            usados_adq.add(row["_id_adq"])
            usados_ctp.add(row["_id_ctp"])
            linhas.append(row)
        if not linhas:
            return
        df = pd.DataFrame(linhas)
        df["nivel"] = NIVEL_APROX
        df["regra"] = nome
        df["confianca"] = confianca
        pares_total.append(df)
        pend_adq = pend_adq[~pend_adq["_id"].isin(df["_id_adq"])]
        pend_ctp = pend_ctp[~pend_ctp["_id"].isin(df["_id_ctp"])]

    _tenta("Valor + mesma data", 80, lambda d: (
        (d["valor_adq"] - d["valor_ctp"]).abs().le(0.0101) & (d["data_adq"] == d["data_ctp"])
    ))
    _tenta("Valor + data ±1 dia", 70, lambda d: (
        (d["valor_adq"] - d["valor_ctp"]).abs().le(0.0101)
        & (d["data_adq"] - d["data_ctp"]).abs().dt.days.le(1)
    ))
    _tenta("Valor + data ±2 dias", 65, lambda d: (
        (d["valor_adq"] - d["valor_ctp"]).abs().le(0.0101)
        & (d["data_adq"] - d["data_ctp"]).abs().dt.days.le(2)
    ))
    _tenta("Valor + Parcela (aprox.)", 65, lambda d: (
        (d["valor_adq"] - d["valor_ctp"]).abs().le(0.0101)
        & d["parcela_adq"].notna() & (d["parcela_adq"] == d["parcela_ctp"])
    ))
    _tenta("TID sem zeros à esquerda", 95, lambda d: (
        d["tid_adq"].fillna("").str.lstrip("0").ne("")
        & (d["tid_adq"].fillna("").str.lstrip("0") == d["tid_ctp"].fillna("").str.lstrip("0"))
    ))
    _tenta("NSU sem zeros à esquerda", 95, lambda d: (
        d["nsu_adq"].fillna("").str.lstrip("0").ne("")
        & (d["nsu_adq"].fillna("").str.lstrip("0") == d["nsu_ctp"].fillna("").str.lstrip("0"))
    ))
    _tenta("TID – últimos 4 dígitos", 60, lambda d: (
        d["tid_adq"].fillna("").str.len().ge(4)
        & (d["tid_adq"].fillna("").str[-4:] == d["tid_ctp"].fillna("").str[-4:])
    ))
    _tenta("NSU – últimos 6 dígitos", 60, lambda d: (
        d["nsu_adq"].fillna("").str.len().ge(6)
        & (d["nsu_adq"].fillna("").str[-6:] == d["nsu_ctp"].fillna("").str[-6:])
    ))
    _tenta("ARP parcial", 70, lambda d: d.apply(
        lambda r: bool(r["arp_adq"]) and not pd.isna(r["arp_adq"]) and not pd.isna(r["arp_ctp"])
        and (str(r["arp_adq"]) in str(r["arp_ctp"]) or str(r["arp_ctp"]) in str(r["arp_adq"])),
        axis=1,
    ))
    _tenta("Valor (± R$ 0,01)", 60, lambda d: (d["valor_adq"] - d["valor_ctp"]).abs().le(0.0101))

    # Similaridade textual do memo — último recurso, MUITO mais custoso
    # (loop aninhado O(n×m) com difflib) — usa um limite próprio, bem menor.
    if not pend_adq.empty and not pend_ctp.empty and len(pend_adq) * len(pend_ctp) <= limite_similaridade:
        usados_ctp: set = set()
        linhas = []
        for _, ra in pend_adq.iterrows():
            melhor, melhor_score = None, 0.0
            for _, rc in pend_ctp.iterrows():
                if rc["_id"] in usados_ctp:
                    continue
                score = difflib.SequenceMatcher(None, ra["memo"].upper(), rc["memo"].upper()).ratio()
                if score > melhor_score:
                    melhor, melhor_score = rc, score
            if melhor is not None and melhor_score >= 0.85:
                linhas.append({
                    "_id_adq": ra["_id"], "_id_ctp": melhor["_id"],
                    "valor_adq": ra["valor"], "valor_ctp": melhor["valor"],
                    "data_adq": ra["data"], "data_ctp": melhor["data"],
                    "numero_documento_adq": ra.get("numero_documento"),
                    "numero_documento_ctp": melhor.get("numero_documento"),
                    "memo_adq": ra["memo"], "memo_ctp": melhor["memo"],
                    "nivel": NIVEL_APROX, "regra": "Similaridade textual (memo)",
                    "confianca": 60, "_score_similaridade": round(melhor_score, 2),
                })
                usados_ctp.add(melhor["_id"])
        if linhas:
            df = pd.DataFrame(linhas)
            pares_total.append(df)
            pend_adq = pend_adq[~pend_adq["_id"].isin(df["_id_adq"])]
            pend_ctp = pend_ctp[~pend_ctp["_id"].isin(df["_id_ctp"])]

    matched = pd.concat(pares_total, ignore_index=True) if pares_total else pd.DataFrame()
    return matched, pend_adq, pend_ctp


# ---------------------------------------------------------------------------
# Motor principal
# ---------------------------------------------------------------------------

def waterfall_match(
    df_adq: pd.DataFrame,
    df_ctp: pd.DataFrame,
    escopo: str = "",
    habilitar_texto: bool = True,
    habilitar_aproximado: bool = True,
    limite_pares_texto: int = 90_000,       # ex.: 300 x 300 — busca em texto, ~seg.
    limite_pares_aproximado: int = 250_000,  # regras vetorizadas (rápidas)
    limite_pares_similaridade: int = 10_000,  # ex.: 100 x 100 — difflib, mais lento
    progress_cb=None,
):
    """Executa a cascata completa (Níveis 1-9) e retorna:
    (pareados, sobra_adq, sobra_ctp, info) — info é um dict com flags
    'texto_pulado'/'similaridade_pulada' quando essas etapas foram
    puladas por excederem o limite de volume (não por falta de match).
    `escopo` (ex.: 'PAG'/'REC') entra no código de conciliação gerado."""
    pend_adq = df_adq.copy()
    pend_ctp = df_ctp.copy()
    pareados = []

    for regra in REGRAS:
        if pend_adq.empty or pend_ctp.empty:
            break
        campos = list(regra.campos)
        if regra.permite_muitos_para_um:
            pares = _casar_muitos_para_um(pend_adq, pend_ctp, campos)
        else:
            pares = _casar_1_para_1(pend_adq, pend_ctp, campos)
        if pares.empty:
            continue
        pares = pares.copy()
        pares["nivel"] = regra.nivel
        pares["regra"] = regra.nome
        pares["confianca"] = regra.confianca
        pareados.append(pares)
        pend_adq = pend_adq[~pend_adq["_id"].isin(pares["_id_adq"])]
        pend_ctp = pend_ctp[~pend_ctp["_id"].isin(pares["_id_ctp"])]
        if progress_cb:
            progress_cb(regra.nome, len(pares), len(pend_adq), len(pend_ctp))

    info = {"texto_pulado": False, "similaridade_pulada": False}

    if habilitar_texto:
        if len(pend_adq) * len(pend_ctp) > limite_pares_texto:
            info["texto_pulado"] = True
        else:
            pares_txt, pend_adq, pend_ctp = _busca_texto(pend_adq, pend_ctp, limite_pares_texto)
            if not pares_txt.empty:
                pareados.append(pares_txt)
                if progress_cb:
                    progress_cb("Busca em texto", len(pares_txt), len(pend_adq), len(pend_ctp))

    if habilitar_aproximado:
        if len(pend_adq) * len(pend_ctp) > limite_pares_similaridade:
            info["similaridade_pulada"] = True
        pares_aprox, pend_adq, pend_ctp = _fallback_aproximado(
            pend_adq, pend_ctp, limite_pares_aproximado, limite_pares_similaridade
        )
        if not pares_aprox.empty:
            pareados.append(pares_aprox)
            if progress_cb:
                progress_cb("Fallback aproximado", len(pares_aprox), len(pend_adq), len(pend_ctp))

    matched = pd.concat(pareados, ignore_index=True, sort=False) if pareados else pd.DataFrame()
    if not matched.empty:
        matched["manual"] = False
        matched["observacao"] = ""
        matched["codigo_conciliacao"] = [
            gerar_codigo_conciliacao(a, c, escopo)
            for a, c in zip(matched["_id_adq"], matched["_id_ctp"])
        ]
    return matched, pend_adq, pend_ctp, info


def construir_par_manual(row_adq: pd.Series, row_ctp: pd.Series, escopo: str, observacao: str = "") -> dict:
    """Monta um registro de match manual no MESMO formato das linhas
    produzidas por `waterfall_match` (colunas *_adq / *_ctp), para poder ser
    concatenado direto na tabela de pares automáticos."""
    par: dict = {}
    for col in row_adq.index:
        par[f"{col}_adq"] = row_adq[col]
    for col in row_ctp.index:
        par[f"{col}_ctp"] = row_ctp[col]
    par["nivel"] = 0
    par["regra"] = "Match Manual (usuário)"
    par["confianca"] = 100
    par["manual"] = True
    par["observacao"] = observacao
    if pd.notna(row_adq.get("valor")) and pd.notna(row_ctp.get("valor")):
        par["_dif_valor"] = abs(row_adq["valor"] - row_ctp["valor"])
    else:
        par["_dif_valor"] = None
    if pd.notna(row_adq.get("data")) and pd.notna(row_ctp.get("data")):
        par["_dif_dias"] = abs((row_adq["data"] - row_ctp["data"]).days)
    else:
        par["_dif_dias"] = None
    par["codigo_conciliacao"] = gerar_codigo_conciliacao(row_adq["_id"], row_ctp["_id"], escopo)
    return par
