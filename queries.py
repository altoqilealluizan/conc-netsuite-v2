"""
queries.py
==========
Consultas SuiteQL do app de conciliação de contas transitórias.

Todas as consultas abaixo foram validadas contra a base real (jul/2026):
  - Tipos existentes nas transitórias de pagamento: apenas 'CustPymt' e 'Journal'.
  - Regra DOC: CustPymt -> t.tranid | Journal -> tl.custcolcustcol_n_pagamento.
  - TID/NSU/ARP nos Pagamentos: campos de corpo custbody_nscs_tid/nsu/arp.
  - TID/NSU/ARP na Adquirente/Recebimentos: extraídos do memo da linha via
    REGEXP_SUBSTR com flag 'i' (memos têm caixa mista: "PARCELA 1" / "Parcela 2",
    prefixo opcional "RA:", e podem omitir PARCELA/NSU/ARP).
  - Datas sempre formatadas em ISO (TO_CHAR ... 'YYYY-MM-DD') para evitar
    ambiguidade DD/MM vs MM/DD na desserialização.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

# ---------------------------------------------------------------------------
# Contas contábeis (IDs internos validados)
# ---------------------------------------------------------------------------

CONTAS_PAGAMENTOS = {
    2247: "1.1.2.01.088 TRANSIT. PAGAMENTO (CARTÃO) - S3ENG",
    2248: "1.1.2.01.089 TRANSIT. PAGAMENTO (YAPAY) - S3ENG",
    2252: "1.1.2.01.100 TRANSIT. PAGAMENTO (CARTÃO) - INEXT",
    2253: "1.1.2.01.101 TRANSIT. PAGAMENTO (YAPAY) - INEXT",
    2256: "1.1.2.01.104 TRANSIT. PAGAMENTO (CARTÃO) - EDUCATION",
    2257: "1.1.2.01.105 TRANSIT. PAGAMENTO (YAPAY) - EDUCATION",
    2260: "1.1.2.01.108 TRANSIT. PAGAMENTO (CARTÃO) - MN",
    2261: "1.1.2.01.109 TRANSIT. PAGAMENTO (YAPAY) - MN",
    2265: "1.1.2.01.112 TRANSIT. PAGAMENTO (CARTÃO) - QIHUB",
    2264: "1.1.2.01.113 TRANSIT. PAGAMENTO (YAPAY) - QIHUB",
}

CONTAS_RECEBIMENTOS = {
    2249: "1.1.2.01.098 TRANSIT. RECEBIMENTO (CARTÃO) - S3ENG",
    2250: "1.1.2.01.099 TRANSIT. RECEBIMENTO (YAPAY) - S3ENG",
    2254: "1.1.2.01.102 TRANSIT. RECEBIMENTO (CARTÃO) - INEXT",
    2255: "1.1.2.01.103 TRANSIT. RECEBIMENTO (YAPAY) - INEXT",
    2258: "1.1.2.01.106 TRANSIT. RECEBIMENTO (CARTÃO) - EDUCATION",
    2259: "1.1.2.01.107 TRANSIT. RECEBIMENTO (YAPAY) - EDUCATION",
    2262: "1.1.2.01.110 TRANSIT. RECEBIMENTO (CARTÃO) - MN",
    2263: "1.1.2.01.111 TRANSIT. RECEBIMENTO (YAPAY) - MN",
    2266: "1.1.2.01.114 TRANSIT. RECEBIMENTO (CARTÃO) - QIHUB",
    2267: "1.1.2.01.115 TRANSIT. RECEBIMENTO (YAPAY) - QIHUB",
}

CONTAS_ADQUIRENTE = {
    1338: "1.1.2.01.005 ADQUIRENTE - CIELO",
    1339: "1.1.2.01.006 ADQUIRENTE - YAPAY",
    1340: "1.1.2.01.007 ADQUIRENTE - REDE",
    2582: "1.1.2.01.010 ADQUIRENTE - SAFRAPAY",
    2575: "1.1.2.01.116 ADQUIRENTE - YAPAY (Educ)",
    2576: "1.1.2.01.117 ADQUIRENTE - YAPAY (QiHub)",
    2577: "1.1.2.01.118 ADQUIRENTE - YAPAY (MN)",
}

# ---------------------------------------------------------------------------
# Mapeamento conta -> subsidiária
# ---------------------------------------------------------------------------
# Detecta a subsidiária a partir do nome da conta (sufixo "- S3ENG", "- MN",
# parênteses "(Educ)"/"(QiHub)" etc.) usando a MESMA função para os nomes de
# subsidiária vindos do NetSuite — assim o filtro da sidebar e o filtro de
# contas usam exatamente o mesmo critério. Contas sem nenhuma subsidiária
# detectada (ex.: ADQUIRENTE - CIELO/YAPAY/REDE/SAFRAPAY, que valem para toda
# a operação) aparecem para QUALQUER subsidiária selecionada.
_SUBSID_ALIASES = {
    "S3ENG": "S3ENG",
    "INEXT": "INEXT",
    "EDUCATION": "EDUCATION",
    "EDUC": "EDUCATION",
    "QIHUB": "QIHUB",
    "MN": "MN",
}


def detectar_subsidiaria(texto: str) -> Optional[str]:
    """Detecta o código interno de subsidiária a partir de um texto (nome de
    conta OU nome de subsidiária do NetSuite). Usa fronteira de
    palavra/número para evitar falso positivo (ex.: 'MN' não deve casar
    dentro de 'ALUMNI' ou similar)."""
    texto_up = (texto or "").upper()
    for alias, codigo in _SUBSID_ALIASES.items():
        if re.search(rf"(?<![A-Z0-9]){alias}(?![A-Z0-9])", texto_up):
            return codigo
    return None


def _mapa_subsidiarias(mapa_contas: dict) -> dict:
    return {cid: detectar_subsidiaria(nome) for cid, nome in mapa_contas.items()}


# conta_id -> código de subsidiária (ou None = vale para todas)
CONTA_SUBSIDIARIA: dict = {
    **_mapa_subsidiarias(CONTAS_PAGAMENTOS),
    **_mapa_subsidiarias(CONTAS_RECEBIMENTOS),
    **_mapa_subsidiarias(CONTAS_ADQUIRENTE),
}


def contas_por_subsidiaria(mapa_contas: dict, subs_codigos: Optional[Iterable[str]]) -> dict:
    """Filtra um dicionário de contas {id: nome} pelas subsidiárias
    selecionadas na sidebar. Contas sem subsidiária detectada (globais,
    ex.: adquirentes genéricos) aparecem sempre, independente do filtro."""
    if not subs_codigos:
        return dict(mapa_contas)
    codigos = set(subs_codigos)
    return {
        cid: nome
        for cid, nome in mapa_contas.items()
        if CONTA_SUBSIDIARIA.get(cid) is None or CONTA_SUBSIDIARIA.get(cid) in codigos
    }


# ---------------------------------------------------------------------------
# Blocos SQL reutilizáveis
# ---------------------------------------------------------------------------

# Regra DOC validada: o Journal de baixa referencia o pagamento na coluna de
# linha "Nº do pagamento" (custcolcustcol_n_pagamento).
SQL_DOC = (
    "CASE WHEN t.type = 'CustPymt' THEN t.tranid "
    "ELSE tl.custcolcustcol_n_pagamento END"
)

# Extração de campos do memorando (flag 'i' é obrigatória: caixa mista na base).
# Padrão validado: "RA: TID xxx | NSU yyy | ARP zzz | Parcela n" (PARCELA opcional)
def _rx(campo: str, pattern: str) -> str:
    return f"REGEXP_SUBSTR(tl.memo, '{pattern}', 1, 1, 'i', 1) AS {campo}"


SQL_MEMO_TID = "REGEXP_SUBSTR(tl.memo, 'TID ([^ |]+)', 1, 1, 'i', 1)"
SQL_MEMO_NSU = "REGEXP_SUBSTR(tl.memo, 'NSU ([^ |]+)', 1, 1, 'i', 1)"
SQL_MEMO_ARP = "REGEXP_SUBSTR(tl.memo, 'ARP ([^ |]+)', 1, 1, 'i', 1)"
SQL_MEMO_PARCELA = "REGEXP_SUBSTR(tl.memo, 'PARCELA ([^ |]+)', 1, 1, 'i', 1)"
SQL_MEMO_FATURA = "REGEXP_SUBSTR(tl.memo, 'FATURA ([^ |]+)', 1, 1, 'i', 1)"
SQL_MEMO_COBRANCA = "REGEXP_SUBSTR(tl.memo, 'COBRAN.A ([^ |]+)', 1, 1, 'i', 1)"

# Chave de conciliação normalizada (trim + uppercase) a partir do memorando
SQL_CHAVE_MEMO = (
    f"UPPER(TRIM({SQL_MEMO_TID})) || '|' || "
    f"UPPER(TRIM({SQL_MEMO_NSU})) || '|' || "
    f"UPPER(TRIM({SQL_MEMO_ARP}))"
)

# Chave de conciliação a partir dos campos de corpo (Pagamentos / CustPymt)
SQL_CHAVE_BODY = (
    "UPPER(TRIM(t.custbody_nscs_tid)) || '|' || "
    "UPPER(TRIM(t.custbody_nscs_nsu)) || '|' || "
    "UPPER(TRIM(t.custbody_nscs_arp))"
)


# ---------------------------------------------------------------------------
# Helpers de filtro (sempre com sanitização mínima)
# ---------------------------------------------------------------------------

def _ids(seq: Iterable[int]) -> str:
    return ",".join(str(int(x)) for x in seq)


def _f_periodo(dt_ini: Optional[str], dt_fim: Optional[str]) -> str:
    """Filtro de período em t.trandate. Datas em 'YYYY-MM-DD'."""
    sql = ""
    if dt_ini:
        sql += f" AND t.trandate >= TO_DATE('{dt_ini}', 'YYYY-MM-DD')"
    if dt_fim:
        sql += f" AND t.trandate <= TO_DATE('{dt_fim}', 'YYYY-MM-DD')"
    return sql


def _f_subsidiaria(subs: Optional[Iterable[int]]) -> str:
    return f" AND tl.subsidiary IN ({_ids(subs)})" if subs else ""


_FROM_BASE = """
FROM transactionline tl
JOIN "transaction" t
  ON t.id = tl.transaction
JOIN transactionaccountingline tal
  ON tal.transaction = tl.transaction AND tal.transactionline = tl.id
"""
# Obs.: o alias "transaction" sem aspas também funciona no SuiteQL; as aspas
# são mantidas por segurança contra palavra reservada.


# ---------------------------------------------------------------------------
# 0. Consultas de apoio (carga dinâmica de dimensões)
# ---------------------------------------------------------------------------

def q_subsidiarias() -> str:
    return "SELECT id, name, fullname FROM subsidiary ORDER BY id"


def q_contas(conta_ids: Iterable[int]) -> str:
    return (
        "SELECT id, acctnumber, accountsearchdisplayname AS nome "
        f"FROM account WHERE id IN ({_ids(conta_ids)}) ORDER BY acctnumber"
    )


# ---------------------------------------------------------------------------
# 1. ABA PAGAMENTOS
# ---------------------------------------------------------------------------
# ATENÇÃO ao usar filtro de data aqui: o saldo por DOC soma o CustPymt e o
# Journal de baixa, que podem cair em datas diferentes (ex.: pagamento em
# jan/25, baixa em dez/25 — ver caso PYMT17073 validado). Filtrar por período
# pode fazer um DOC que na verdade já zerou aparecer como "não zerado" só
# porque uma das duas pontas ficou fora da janela. "Todo o período" é o modo
# recomendado para a visão de exceções; use o filtro de data apenas para
# análises pontuais, cientes desse efeito de borda.

def q_pagamentos_kpi(contas: Iterable[int], subs=None, dt_ini=None, dt_fim=None) -> str:
    """KPIs gerais + universo de DOCs abertos (saldo <> 0 pela regra DOC)."""
    return f"""
SELECT
  COUNT(*)                              AS linhas,
  COUNT(DISTINCT {SQL_DOC})             AS docs,
  SUM(NVL(tal.debit, 0))                AS total_debito,
  SUM(NVL(tal.credit, 0))               AS total_credito,
  SUM(NVL(tal.debit, 0)) - SUM(NVL(tal.credit, 0)) AS saldo
{_FROM_BASE}
WHERE tal.account IN ({_ids(contas)}){_f_subsidiaria(subs)}{_f_periodo(dt_ini, dt_fim)}
"""


def q_pagamentos_consolidado(
    contas: Iterable[int],
    subs=None,
    somente_abertos: bool = True,
    dt_ini=None,
    dt_fim=None,
) -> str:
    """Visão consolidada por DOC. Por padrão traz só DOCs que não zeram
    (universo validado: 25 DOCs em 255 mil, sem filtro de data).

    ATENÇÃO (limitação do NetSuite validada nesta sessão): com o toggle
    'somente_abertos' desligado, o GROUP BY produz ~125 mil grupos — e
    ORDER BY numa coluna AGREGADA (saldo, um SUM) nessa escala faz a API
    REST do NetSuite retornar 0 linhas silenciosamente (a paginação segue
    dizendo que há mais páginas, mas o array de dados vem vazio). Ordenar
    pela própria chave do GROUP BY (o DOC, coluna 1) funciona normalmente
    em qualquer escala, então só usamos ORDER BY saldo quando o HAVING já
    reduziu o resultado a poucas linhas (a visão de exceções)."""
    having = (
        "HAVING SUM(NVL(tal.debit,0)) - SUM(NVL(tal.credit,0)) <> 0"
        if somente_abertos else ""
    )
    order_by = "ORDER BY 4 DESC" if somente_abertos else "ORDER BY 1"
    return f"""
SELECT
  {SQL_DOC}                                        AS doc,
  SUM(NVL(tal.debit, 0))                           AS valor_debito,
  SUM(NVL(tal.credit, 0))                          AS valor_credito,
  SUM(NVL(tal.debit, 0)) - SUM(NVL(tal.credit, 0)) AS saldo,
  COUNT(*)                                         AS qtd_linhas,
  TO_CHAR(MIN(t.trandate), 'YYYY-MM-DD')           AS primeira_data,
  TO_CHAR(MAX(t.trandate), 'YYYY-MM-DD')           AS ultima_data
{_FROM_BASE}
WHERE tal.account IN ({_ids(contas)}){_f_subsidiaria(subs)}{_f_periodo(dt_ini, dt_fim)}
GROUP BY {SQL_DOC}
{having}
{order_by}
"""


def q_pagamentos_detalhe_doc(contas: Iterable[int], doc: str) -> str:
    """Razão detalhado de um DOC específico (drill-down), com todas as
    colunas da especificação. As duas colunas 'Número do documento' são
    mantidas distintas: numero_documento_1 (tranid) e numero_documento_2
    (Nº do pagamento da linha do Journal)."""
    doc_safe = doc.replace("'", "''")
    return f"""
SELECT
  t.id                                        AS id_interno,
  t.type                                      AS tipo,
  t.tranid                                    AS numero_documento_1,
  tl.custcolcustcol_n_pagamento               AS numero_documento_2,
  t.externalid                                AS id_externo,
  tl.id                                       AS id_linha,
  t.transactionnumber                         AS numero_transacao,
  TO_CHAR(t.trandate, 'YYYY-MM-DD')           AS data,
  TO_CHAR(t.asofdate, 'YYYY-MM-DD')           AS a_partir_da_data,
  BUILTIN.DF(t.postingperiod)                 AS periodo,
  BUILTIN.DF(t.entity)                        AS nome,
  BUILTIN.DF(tal.account)                     AS conta,
  NVL(tl.memo, t.memo)                        AS memorando,
  tal.debit                                   AS valor_debito,
  tal.credit                                  AS valor_credito,
  NVL(tal.debit, 0) - NVL(tal.credit, 0)      AS valor,
  t.custbody_psg_ei_inbound_edocument         AS doc_eletronico_entrada,
  tal.posting                                 AS contabilizacao,
  BUILTIN.DF(tl.subsidiary)                   AS subsidiaria,
  BUILTIN.DF(t.status)                        AS status,
  t.custbody_psg_ei_status                    AS status_doc_eletronico,
  t.source                                    AS origem,
  tl.createdfrom                              AS criar_a_partir_de,
  TO_CHAR(t.createddate, 'YYYY-MM-DD')        AS data_criacao,
  t.custbody_nscs_tid                         AS tid,
  t.custbody_nscs_nsu                         AS nsu,
  t.custbody_nscs_arp                         AS arp,
  NVL(t.custbodycustbody_nscs_idcobranca,
      tl.custcolcustcol_id_cobranca)          AS id_cobranca_vindi,
  tl.custcolcustcol_n_pagamento               AS n_pagamento,
  BUILTIN.DF(t.createdby)                     AS criado_por
{_FROM_BASE}
WHERE tal.account IN ({_ids(contas)})
  AND ( (t.type = 'CustPymt' AND t.tranid = '{doc_safe}')
     OR (t.type = 'Journal'  AND tl.custcolcustcol_n_pagamento = '{doc_safe}') )
ORDER BY t.trandate, t.id, tl.id
"""


# ---------------------------------------------------------------------------
# 2. ABA RECEBIMENTOS
# ---------------------------------------------------------------------------

def q_recebimentos_consolidado(
    contas: Iterable[int], dt_ini=None, dt_fim=None, subs=None
) -> str:
    """Consolidado Conta > Data (subtotais por data são calculados no app)."""
    return f"""
SELECT
  a.acctnumber                                     AS conta_numero,
  a.accountsearchdisplayname                       AS conta,
  TO_CHAR(t.trandate, 'YYYY-MM-DD')                AS data,
  SUM(NVL(tal.debit, 0))                           AS valor_debito,
  SUM(NVL(tal.credit, 0))                          AS valor_credito,
  SUM(NVL(tal.debit, 0)) - SUM(NVL(tal.credit, 0)) AS valor
{_FROM_BASE}
LEFT JOIN account a ON a.id = tal.account
WHERE tal.account IN ({_ids(contas)})
  {_f_periodo(dt_ini, dt_fim)}{_f_subsidiaria(subs)}
GROUP BY a.acctnumber, a.accountsearchdisplayname, TO_CHAR(t.trandate, 'YYYY-MM-DD')
ORDER BY 1, 3
"""


def q_recebimentos_detalhe(
    contas: Iterable[int], dt_ini=None, dt_fim=None, subs=None
) -> str:
    """Razão detalhado da transitória de recebimento (colunas da especificação)."""
    return f"""
SELECT
  t.id                                    AS id_interno,
  t.type                                  AS tipo,
  t.tranid                                AS numero_documento,
  tl.id                                   AS id_linha,
  t.transactionnumber                     AS numero_transacao,
  TO_CHAR(t.trandate, 'YYYY-MM-DD')       AS data,
  BUILTIN.DF(t.postingperiod)             AS periodo,
  BUILTIN.DF(t.entity)                    AS nome,
  BUILTIN.DF(tal.account)                 AS conta,
  NVL(tl.memo, t.memo)                    AS memorando,
  tal.debit                               AS valor_debito,
  tal.credit                              AS valor_credito,
  NVL(tal.debit,0) - NVL(tal.credit,0)    AS valor,
  tal.posting                             AS contabilizacao,
  BUILTIN.DF(tl.subsidiary)               AS subsidiaria,
  TO_CHAR(t.createddate, 'YYYY-MM-DD')    AS data_criacao,
  tl.custcolcustcoldata_idsaque           AS id_saque,
  TO_CHAR(tl.custcoldata_recebimento, 'YYYY-MM-DD') AS data_recebimento,
  BUILTIN.DF(t.createdby)                 AS criado_por,
  t.externalid                            AS id_externo
{_FROM_BASE}
WHERE tal.account IN ({_ids(contas)})
  {_f_periodo(dt_ini, dt_fim)}{_f_subsidiaria(subs)}
ORDER BY tal.account, t.trandate, t.id, tl.id
"""


# ---------------------------------------------------------------------------
# 3. ABA ADQUIRENTE
# ---------------------------------------------------------------------------

def q_adquirente_consolidado(
    contas: Iterable[int], dt_ini=None, dt_fim=None, subs=None
) -> str:
    return f"""
SELECT
  a.acctnumber                                     AS conta_numero,
  a.accountsearchdisplayname                       AS conta,
  TO_CHAR(t.trandate, 'YYYY-MM-DD')                AS data,
  SUM(NVL(tal.debit, 0))                           AS valor_debito,
  SUM(NVL(tal.credit, 0))                          AS valor_credito,
  SUM(NVL(tal.debit, 0)) - SUM(NVL(tal.credit, 0)) AS valor,
  COUNT(*)                                         AS qtd_linhas
{_FROM_BASE}
LEFT JOIN account a ON a.id = tal.account
WHERE tal.account IN ({_ids(contas)})
  {_f_periodo(dt_ini, dt_fim)}{_f_subsidiaria(subs)}
GROUP BY a.acctnumber, a.accountsearchdisplayname, TO_CHAR(t.trandate, 'YYYY-MM-DD')
ORDER BY 1, 3
"""


def q_adquirente_detalhe(
    contas: Iterable[int], dt_ini=None, dt_fim=None, subs=None
) -> str:
    """Razão detalhado com colunas calculadas do memorando (parsing server-side).
    ATENÇÃO: exige filtro de período — a base completa tem ~388 mil linhas."""
    return f"""
SELECT
  t.id                                    AS id_interno,
  t.type                                  AS tipo,
  t.tranid                                AS numero_documento,
  tl.id                                   AS id_linha,
  t.transactionnumber                     AS numero_transacao,
  TO_CHAR(t.trandate, 'YYYY-MM-DD')       AS data,
  BUILTIN.DF(t.postingperiod)             AS periodo,
  BUILTIN.DF(t.entity)                    AS nome,
  BUILTIN.DF(tal.account)                 AS conta,
  NVL(tl.memo, t.memo)                    AS memorando,
  tal.debit                               AS valor_debito,
  tal.credit                              AS valor_credito,
  NVL(tal.debit,0) - NVL(tal.credit,0)    AS valor,
  tal.posting                             AS contabilizacao,
  BUILTIN.DF(tl.subsidiary)               AS subsidiaria,
  TO_CHAR(t.createddate, 'YYYY-MM-DD')    AS data_criacao,
  tl.custcolcustcoldata_idsaque           AS id_saque,
  TO_CHAR(tl.custcoldata_recebimento, 'YYYY-MM-DD') AS data_recebimento,
  tl.custcolcustcol_n_pagamento           AS n_pagamento,
  tl.custcolcustcol_id_fatura             AS id_fatura_vindi,
  NVL(t.custbody_nscs_faturavindi,
      tl.custcolcustcol_id_fatura)        AS id_da_fatura_vindi,
  BUILTIN.DF(t.createdby)                 AS criado_por,
  {SQL_MEMO_TID}                          AS tid,
  {SQL_MEMO_NSU}                          AS nsu,
  {SQL_MEMO_ARP}                          AS arp,
  {SQL_MEMO_PARCELA}                      AS parcela,
  {SQL_MEMO_FATURA}                       AS fatura,
  {SQL_MEMO_COBRANCA}                     AS cobranca
{_FROM_BASE}
WHERE tal.account IN ({_ids(contas)})
  {_f_periodo(dt_ini, dt_fim)}{_f_subsidiaria(subs)}
ORDER BY t.trandate, t.id, tl.id
"""


# ---------------------------------------------------------------------------
# 4. MÓDULO DE CONCILIAÇÃO (TID + NSU + ARP)
# ---------------------------------------------------------------------------

def _src_memo(contas: Iterable[int], origem: str, dt_ini, dt_fim, subs) -> str:
    """Fonte de dados com chave extraída do memorando da linha (débito e
    crédito separados para permitir comparação lado a lado entre origens)."""
    return f"""
SELECT
  {SQL_CHAVE_MEMO}                       AS chave,
  '{origem}'                             AS origem,
  NVL(tal.debit, 0)                      AS deb,
  NVL(tal.credit, 0)                     AS cred,
  tl.subsidiary                          AS subsidiaria_id,
  tal.account                            AS conta_id,
  t.trandate                             AS data
{_FROM_BASE}
WHERE tal.account IN ({_ids(contas)})
  AND tl.memo LIKE '%TID%'
  {_f_periodo(dt_ini, dt_fim)}{_f_subsidiaria(subs)}
"""


def _src_body_pagamentos(contas: Iterable[int], dt_ini, dt_fim, subs) -> str:
    """Fonte Pagamentos: chave nos campos de corpo do CustPymt."""
    return f"""
SELECT
  {SQL_CHAVE_BODY}                       AS chave,
  'PAGAMENTOS'                           AS origem,
  NVL(tal.debit, 0)                      AS deb,
  NVL(tal.credit, 0)                     AS cred,
  tl.subsidiary                          AS subsidiaria_id,
  tal.account                            AS conta_id,
  t.trandate                             AS data
{_FROM_BASE}
WHERE tal.account IN ({_ids(contas)})
  AND t.type = 'CustPymt'
  AND t.custbody_nscs_tid IS NOT NULL
  {_f_periodo(dt_ini, dt_fim)}{_f_subsidiaria(subs)}
"""


def _fonte_conciliacao(escopo: str, dt_ini, dt_fim, subs) -> str:
    """Monta a fonte (UNION ALL) conforme o escopo escolhido no app."""
    adq = _src_memo(CONTAS_ADQUIRENTE, "ADQUIRENTE", dt_ini, dt_fim, subs)
    if escopo == "ADQ":
        return adq
    if escopo == "ADQ_PAG":
        pag = _src_body_pagamentos(CONTAS_PAGAMENTOS, dt_ini, dt_fim, subs)
        return f"{adq}\nUNION ALL\n{pag}"
    if escopo == "ADQ_REC":
        rec = _src_memo(CONTAS_RECEBIMENTOS, "RECEBIMENTOS", dt_ini, dt_fim, subs)
        return f"{adq}\nUNION ALL\n{rec}"
    raise ValueError(f"Escopo inválido: {escopo}")


# ---------------------------------------------------------------------------
# Regra de divergência por escopo (dupla partida, validada contra a base):
#   ADQ     -> saldo (débitos − créditos) da chave dentro da adquirente <> 0.
#              Entrada da venda vs baixa do saque na mesma família de contas.
#              Inclui parcelas legitimamente a receber -> classificar por aging.
#   ADQ_PAG -> débitos ADQ <> débitos PAG. As duas origens registram a MESMA
#              transação a débito (CustPymt debita a transitória de pagamento;
#              o Journal de venda debita a adquirente); a checagem é se a venda
#              entrou nas duas pontas com o mesmo valor.
#   ADQ_REC -> créditos ADQ <> débitos REC. O saque credita a adquirente e
#              debita a transitória de recebimento.
# ---------------------------------------------------------------------------

_AGG_LADOS = """
    SUM(CASE WHEN origem = 'ADQUIRENTE' THEN deb  ELSE 0 END) AS deb_adq,
    SUM(CASE WHEN origem = 'ADQUIRENTE' THEN cred ELSE 0 END) AS cred_adq,
    SUM(CASE WHEN origem <> 'ADQUIRENTE' THEN deb  ELSE 0 END) AS deb_ctp,
    SUM(CASE WHEN origem <> 'ADQUIRENTE' THEN cred ELSE 0 END) AS cred_ctp
"""


def _expr_divergencia(escopo: str) -> str:
    return {
        "ADQ": "(deb_adq - cred_adq)",
        "ADQ_PAG": "(deb_adq - deb_ctp)",
        "ADQ_REC": "(cred_adq - deb_ctp)",
    }[escopo]


def q_conciliacao_resumo(escopo: str, dt_ini=None, dt_fim=None, subs=None) -> str:
    """KPIs do módulo: total de chaves, conciliadas, divergentes e valor."""
    fonte = _fonte_conciliacao(escopo, dt_ini, dt_fim, subs)
    div = _expr_divergencia(escopo)
    return f"""
SELECT
  COUNT(*)                                              AS total_chaves,
  SUM(CASE WHEN {div} =  0 THEN 1 ELSE 0 END)           AS chaves_ok,
  SUM(CASE WHEN {div} <> 0 THEN 1 ELSE 0 END)           AS chaves_divergentes,
  SUM(CASE WHEN {div} <> 0 THEN {div} ELSE 0 END)       AS valor_divergente
FROM (
  SELECT chave,
{_AGG_LADOS}
  FROM ({fonte})
  GROUP BY chave
)
"""


def q_conciliacao_divergencias(
    escopo: str,
    dt_ini=None,
    dt_fim=None,
    subs=None,
) -> str:
    """Lista de chaves divergentes segundo a regra do escopo, com aging e os
    dois lados expostos (débito/crédito de cada origem) para investigação."""
    fonte = _fonte_conciliacao(escopo, dt_ini, dt_fim, subs)
    div = _expr_divergencia(escopo)
    return f"""
SELECT
  chave,
  {div}                                    AS diferenca,
  deb_adq,
  cred_adq,
  deb_ctp                                  AS deb_contraparte,
  cred_ctp                                 AS cred_contraparte,
  qtd_lancamentos,
  origens,
  TO_CHAR(primeira_data, 'YYYY-MM-DD')     AS primeira_data,
  TO_CHAR(ultima_data, 'YYYY-MM-DD')       AS ultima_data,
  TRUNC(SYSDATE - ultima_data)             AS dias_desde_ultimo,
  subsidiarias,
  contas
FROM (
  SELECT
    chave,
{_AGG_LADOS},
    COUNT(*)                               AS qtd_lancamentos,
    COUNT(DISTINCT origem)                 AS origens,
    MIN(data)                              AS primeira_data,
    MAX(data)                              AS ultima_data,
    COUNT(DISTINCT subsidiaria_id)         AS subsidiarias,
    COUNT(DISTINCT conta_id)               AS contas
  FROM ({fonte})
  GROUP BY chave
)
WHERE {div} <> 0
ORDER BY 1
"""
# NOTA: ordenar por "chave" (coluna 1, não-agregada) em vez de ABS({div}) —
# a mesma limitação do NetSuite validada em q_pagamentos_consolidado (ORDER
# BY numa expressão agregada retorna 0 linhas silenciosamente quando o
# GROUP BY é muito grande) se aplica aqui: o escopo 'Só Adquirente' sem
# filtro de data pode ter ~130 mil chaves divergentes. A ordenação por
# "maior diferença primeiro" que o usuário vê na tela é feita em pandas
# DEPOIS de baixar os dados (ver app.py), não mais no SQL.


def q_conciliacao_detalhe_chave(tid: str) -> str:
    """Drill-down: todos os lançamentos de um TID nas três famílias de contas
    (memo LIKE nas contas Adquirente/Recebimento + custbody nos Pagamentos)."""
    tid_safe = tid.replace("'", "''")
    todas = list(CONTAS_ADQUIRENTE) + list(CONTAS_RECEBIMENTOS)
    return f"""
SELECT * FROM (
SELECT
  'ADQ/REC'                               AS familia,
  t.id                                    AS id_interno,
  t.type                                  AS tipo,
  t.tranid                                AS numero_documento,
  TO_CHAR(t.trandate, 'YYYY-MM-DD')       AS data,
  a.acctnumber                            AS conta_numero,
  a.accountsearchdisplayname              AS conta,
  s.name                                  AS subsidiaria,
  tl.memo                                 AS memorando,
  tal.debit                               AS valor_debito,
  tal.credit                              AS valor_credito,
  NVL(tal.debit,0) - NVL(tal.credit,0)    AS valor
{_FROM_BASE}
LEFT JOIN account a    ON a.id = tal.account
LEFT JOIN subsidiary s ON s.id = tl.subsidiary
WHERE tal.account IN ({_ids(todas)})
  AND UPPER(tl.memo) LIKE '%TID {tid_safe.upper()}%'
UNION ALL
SELECT
  'PAGAMENTOS',
  t.id, t.type, t.tranid,
  TO_CHAR(t.trandate, 'YYYY-MM-DD'),
  a.acctnumber, a.accountsearchdisplayname, s.name,
  NVL(tl.memo, t.memo),
  tal.debit, tal.credit,
  NVL(tal.debit,0) - NVL(tal.credit,0)
{_FROM_BASE}
LEFT JOIN account a    ON a.id = tal.account
LEFT JOIN subsidiary s ON s.id = tl.subsidiary
WHERE tal.account IN ({_ids(CONTAS_PAGAMENTOS)})
  AND UPPER(TRIM(t.custbody_nscs_tid)) = '{tid_safe.upper()}'
)
ORDER BY data, id_interno
"""


# ---------------------------------------------------------------------------
# 5. MATCHING AVANÇADO (waterfall multi-nível) — extrações em lote para
#    alimentar o motor Python (matching.py). Diferente do módulo de
#    conciliação por chave TID+NSU+ARP acima (que agrega e faz 1 comparação
#    de saldo), aqui cada LINHA é candidata a um pareamento individual, então
#    a agregação por chave não serve — precisamos do detalhe linha a linha.
#
# Mapeamento de campos validado contra a base (jul/2026):
#   TID/NSU/ARP  — Pagamentos: custbody_nscs_tid/nsu/arp (corpo).
#                  Adquirente/Recebimentos: regex no memo da linha.
#   Fatura       — prioriza o valor extraído do memo (mais presente na
#                  amostra real); cai para custcolcustcol_id_fatura /
#                  custbody_nscs_faturavindi quando o memo não tem o campo.
#   Parcela      — regex no memo em TODAS as origens (Pagamentos também tem
#                  o padrão "referente a parcela N" no memo do CustPymt).
#   ID da Transação — campo de cross-reference NetSuite validado:
#                  ADQ×PAG: custcolcustcol_n_pagamento (linha da Adquirente)
#                    == tranid do CustPymt (Pagamentos). 1 para 1.
#                  ADQ×REC: custcolcustcoldata_idsaque (linha da Adquirente)
#                    == custcolcustcoldata_idsaque (linha do Recebimento).
#                    N para 1 (várias vendas formam 1 saque/depósito) — o
#                    motor de matching trata esse caso corretamente.
#                  ATENÇÃO: esta é uma escolha de mapeamento — se o time
#                  entender "ID da Transação" como outro campo (ex.: o nº
#                  "Transação" que aparece no memo da Adquirente, aparentemente
#                  um ID interno do gateway Vindi), este é o ponto do código
#                  para ajustar (função q_adquirente_matching_campos abaixo).
# ---------------------------------------------------------------------------

def q_pagamentos_matching(
    contas: Iterable[int], dt_ini: str, dt_fim: str, subs=None
) -> str:
    """Linhas de CustPymt (linha a linha) prontas para o motor de matching.
    Período é OBRIGATÓRIO (volume: ~256 mil linhas na base completa)."""
    return f"""
SELECT
  'PAG-' || t.id || '-' || tl.id            AS _id,
  t.id                                      AS id_interno,
  tl.id                                     AS id_linha,
  t.tranid                                  AS numero_documento,
  t.tranid                                  AS id_transacao,
  t.type                                    AS tipo,
  TO_CHAR(t.trandate, 'YYYY-MM-DD')         AS data,
  NVL(tal.debit, 0)                         AS valor,
  UPPER(TRIM(t.custbody_nscs_tid))          AS tid,
  UPPER(TRIM(t.custbody_nscs_nsu))          AS nsu,
  UPPER(TRIM(t.custbody_nscs_arp))          AS arp,
  UPPER(TRIM(NVL(t.custbody_nscs_faturavindi, tl.custcolcustcol_id_fatura))) AS fatura,
  UPPER(TRIM({SQL_MEMO_PARCELA}))           AS parcela,
  NVL(tl.memo, t.memo)                      AS memo,
  BUILTIN.DF(tal.account)                   AS conta,
  BUILTIN.DF(tl.subsidiary)                 AS subsidiaria
{_FROM_BASE}
WHERE tal.account IN ({_ids(contas)})
  AND t.type = 'CustPymt'
  {_f_periodo(dt_ini, dt_fim)}{_f_subsidiaria(subs)}
"""


def q_recebimentos_matching(
    contas: Iterable[int], dt_ini: str, dt_fim: str, subs=None
) -> str:
    """Linhas de Recebimento (linha a linha) prontas para o motor de
    matching. Período é OBRIGATÓRIO."""
    return f"""
SELECT
  'REC-' || t.id || '-' || tl.id            AS _id,
  t.id                                      AS id_interno,
  tl.id                                     AS id_linha,
  t.tranid                                  AS numero_documento,
  UPPER(TRIM(tl.custcolcustcoldata_idsaque)) AS id_transacao,
  t.type                                    AS tipo,
  TO_CHAR(t.trandate, 'YYYY-MM-DD')         AS data,
  NVL(tal.debit, 0)                         AS valor,
  UPPER(TRIM({SQL_MEMO_TID}))               AS tid,
  UPPER(TRIM({SQL_MEMO_NSU}))               AS nsu,
  UPPER(TRIM({SQL_MEMO_ARP}))               AS arp,
  UPPER(TRIM(NVL(tl.custcolcustcol_id_fatura, {SQL_MEMO_FATURA}))) AS fatura,
  UPPER(TRIM({SQL_MEMO_PARCELA}))           AS parcela,
  NVL(tl.memo, t.memo)                      AS memo,
  BUILTIN.DF(tal.account)                   AS conta,
  BUILTIN.DF(tl.subsidiary)                 AS subsidiaria
{_FROM_BASE}
WHERE tal.account IN ({_ids(contas)})
  {_f_periodo(dt_ini, dt_fim)}{_f_subsidiaria(subs)}
"""


def q_adquirente_matching(
    contas: Iterable[int], dt_ini: str, dt_fim: str, subs=None
) -> str:
    """Linhas de Adquirente (linha a linha) prontas para o motor de
    matching, com AMBOS os candidatos de 'ID da Transação' (n_pagamento e
    id_saque) — o app escolhe qual usar conforme o escopo (ADQ×PAG ou
    ADQ×REC). Período é OBRIGATÓRIO (volume: ~388 mil linhas na base)."""
    return f"""
SELECT
  'ADQ-' || t.id || '-' || tl.id            AS _id,
  t.id                                      AS id_interno,
  tl.id                                     AS id_linha,
  t.tranid                                  AS numero_documento,
  UPPER(TRIM(tl.custcolcustcol_n_pagamento)) AS id_transacao_pag,
  UPPER(TRIM(tl.custcolcustcoldata_idsaque)) AS id_transacao_rec,
  t.type                                    AS tipo,
  TO_CHAR(t.trandate, 'YYYY-MM-DD')         AS data,
  NVL(tal.debit, 0)                         AS valor_debito,
  NVL(tal.credit, 0)                        AS valor_credito,
  UPPER(TRIM({SQL_MEMO_TID}))               AS tid,
  UPPER(TRIM({SQL_MEMO_NSU}))               AS nsu,
  UPPER(TRIM({SQL_MEMO_ARP}))               AS arp,
  UPPER(TRIM(NVL({SQL_MEMO_FATURA}, tl.custcolcustcol_id_fatura))) AS fatura,
  UPPER(TRIM({SQL_MEMO_PARCELA}))           AS parcela,
  NVL(tl.memo, t.memo)                      AS memo,
  BUILTIN.DF(tal.account)                   AS conta,
  BUILTIN.DF(tl.subsidiary)                 AS subsidiaria
{_FROM_BASE}
WHERE tal.account IN ({_ids(contas)})
  AND tl.memo LIKE '%TID%'
  {_f_periodo(dt_ini, dt_fim)}{_f_subsidiaria(subs)}
"""
