"""
app.py
======
Dashboard de conciliação de contas transitórias (NetSuite) — Streamlit.

Execução:
    streamlit run app.py

Abas:
    1. Pagamentos       — razão + consolidado por DOC (regra CustPymt/Journal)
    2. Recebimentos     — razão + consolidado Conta > Data
    3. Adquirente       — razão + colunas calculadas do memorando (TID/NSU/ARP...)
    4. Conciliação      — cruzamento por TID+NSU+ARP com escopo configurável
    5. Matching Avançado — waterfall multi-nível linha-a-linha com classificação
       de confiança (ID da Transação/TID/NSU/ARP/Fatura/Parcela/Valor, busca em
       texto e fallback aproximado) — ver matching.py para a especificação completa.

Arquitetura: toda agregação pesada roda DENTRO do NetSuite via SuiteQL
(a base tem ~650 mil linhas GL); o app baixa apenas KPIs, consolidados
filtrados, exceções e drill-downs sob demanda.
"""

from __future__ import annotations

import io
import os
from datetime import date, timedelta

import pandas as pd
import plotly.express as px
import streamlit as st
from dotenv import load_dotenv

import matching as mt
import queries as q
from netsuite_client import NetSuiteClient, NetSuiteError

# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------

load_dotenv()
CACHE_TTL = int(os.getenv("CACHE_TTL", "600"))

st.set_page_config(
    page_title="Conciliação de Transitórias — NetSuite",
    page_icon="🧮",
    layout="wide",
)

# Alarga as "pills" do multiselect para reduzir truncamento de nomes longos
# de conta (a lista de nomes completos abaixo do widget é o fallback garantido).
st.markdown(
    """
    <style>
    span[data-baseweb="tag"] { max-width: 460px !important; }
    span[data-baseweb="tag"] span[title] { white-space: normal !important; }
    </style>
    """,
    unsafe_allow_html=True,
)

VERMELHO = "#c62828"
VERDE = "#2e7d32"
hoje = date.today()
# Decisão de negócio (2026-07-16): carregar por padrão só o ano corrente;
# "Carregar mais" expande até aqui; nunca buscar antes disso.
ANO_ATUAL_INICIO = date(2026, 1, 1)
DATA_MINIMA_HISTORICO = date(2025, 1, 1)


# ---------------------------------------------------------------------------
# Infra: cliente, cache e utilidades
# ---------------------------------------------------------------------------

@st.cache_resource
def get_client() -> NetSuiteClient:
    return NetSuiteClient(
        account=os.getenv("NS_ACCOUNT", ""),
        consumer_key=os.getenv("NS_CONSUMER_KEY", ""),
        consumer_secret=os.getenv("NS_CONSUMER_SECRET", ""),
        token_id=os.getenv("NS_TOKEN_ID", ""),
        token_secret=os.getenv("NS_TOKEN_SECRET", ""),
    )


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def run_query(sql: str, max_rows: int | None = None) -> pd.DataFrame:
    """Executa SuiteQL com cache (chaveado pelo texto da consulta)."""
    return get_client().suiteql(sql, max_rows=max_rows)


def brl(v) -> str:
    """Formata número no padrão monetário brasileiro."""
    if v is None or pd.isna(v):
        return "—"
    return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _fmt_data_cel(v) -> str:
    """Formata uma célula de data isolada como dd/mm/aaaa (usado no Styler)."""
    if pd.isna(v):
        return "—"
    d = pd.to_datetime(v, errors="coerce")
    return d.strftime("%d/%m/%Y") if pd.notna(d) else str(v)


def estilizar(df: pd.DataFrame, moeda: list[str] = (), data: list[str] = ()):
    """Retorna um pandas Styler pronto para st.dataframe(): colunas de moeda
    exibidas como R$ 0.000,00 e colunas de data como dd/mm/aaaa — SEM alterar
    os valores reais do DataFrame (cálculos, somas e `.map()` de estilo em
    cima do mesmo Styler continuam funcionando com os números originais)."""
    fmt = {}
    for c in moeda:
        if c in df.columns:
            fmt[c] = brl
    for c in data:
        if c in df.columns:
            fmt[c] = _fmt_data_cel
    return df.style.format(fmt)


def to_excel(df: pd.DataFrame, nome_aba: str, moeda: list[str] = (), data: list[str] = ()) -> bytes:
    """Gera um Excel em memória (1 aba) com formato NATIVO do Excel para
    moeda (R$ #.##0,00) e data (dd/mm/aaaa) — os valores continuam numéricos/
    data de verdade na planilha (dá para somar e filtrar no Excel), só o
    formato de exibição da célula muda."""
    d = df.copy()
    for c in data:
        if c in d.columns:
            d[c] = pd.to_datetime(d[c], errors="coerce")
    buf = io.BytesIO()
    with pd.ExcelWriter(
        buf, engine="xlsxwriter", date_format="dd/mm/yyyy", datetime_format="dd/mm/yyyy"
    ) as writer:
        d.to_excel(writer, sheet_name=nome_aba[:31], index=False)
        wb = writer.book
        ws = writer.sheets[nome_aba[:31]]
        fmt_moeda = wb.add_format({"num_format": 'R$ #,##0.00'})
        fmt_data = wb.add_format({"num_format": "dd/mm/yyyy"})
        for i, col in enumerate(d.columns):
            if len(d) > 0:
                maior = d[col].astype(str).str.len().max()
            else:
                maior = None
            # Coluna 100% nula (comum em campos que só existem para um dos
            # escopos, ex. id_transacao_rec no escopo Pagamentos): .astype(str)
            # mantém NaN em vez de virar a string 'nan' — sem isso o int()
            # abaixo quebrava.
            if maior is None or pd.isna(maior):
                maior = 12
            largura = max(12, min(45, int(maior) + 2))
            fmt_col = fmt_moeda if col in moeda else (fmt_data if col in data else None)
            ws.set_column(i, i, largura, fmt_col)
    return buf.getvalue()


def botao_exportar(df: pd.DataFrame, nome: str, key: str, moeda: list[str] = (), data: list[str] = ()) -> None:
    c1, c2 = st.columns(2)
    c1.download_button(
        "⬇️ Excel", to_excel(df, nome, moeda=moeda, data=data),
        file_name=f"{nome}.xlsx", key=f"xlsx_{key}",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    # CSV é texto puro (sem formato nativo) — moeda já sai com decimal=","
    # (padrão BR); datas viram string dd/mm/aaaa antes de exportar.
    df_csv = df.copy()
    for c in data:
        if c in df_csv.columns:
            df_csv[c] = pd.to_datetime(df_csv[c], errors="coerce").dt.strftime("%d/%m/%Y")
    c2.download_button(
        "⬇️ CSV", df_csv.to_csv(index=False, sep=";", decimal=",").encode("utf-8-sig"),
        file_name=f"{nome}.csv", key=f"csv_{key}", mime="text/csv",
    )


def num(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    for c in cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def filtro_periodo(key: str):
    """Widget padrão de filtro de data usado em todas as abas.

    Comportamento (decisão de negócio 2026-07-16): por padrão carrega só o
    ano corrente (2026-01-01 até hoje) — mais rápido e cobre o que
    normalmente importa. Um botão "Carregar mais" expande para incluir
    2025 também. O histórico antes de 2025-01-01 nunca é buscado — nem no
    modo "Período personalizado", onde o seletor de data não deixa
    escolher uma data anterior a essa."""
    chave_2025 = f"{key}_inclui_2025"
    if chave_2025 not in st.session_state:
        st.session_state[chave_2025] = False

    c1, c2 = st.columns([1.3, 2])
    personalizado = c1.toggle("Período personalizado", value=False, key=f"{key}_personalizado")

    if personalizado:
        c3, c4 = st.columns(2)
        dt_ini = c3.date_input(
            "Data início", ANO_ATUAL_INICIO, min_value=DATA_MINIMA_HISTORICO, key=f"{key}_ini"
        ).isoformat()
        dt_fim = c4.date_input(
            "Data fim", hoje, min_value=DATA_MINIMA_HISTORICO, key=f"{key}_fim"
        ).isoformat()
        return dt_ini, dt_fim

    if st.session_state[chave_2025]:
        dt_ini = DATA_MINIMA_HISTORICO.isoformat()
        c2.caption(
            f"📂 Carregando desde {DATA_MINIMA_HISTORICO.strftime('%d/%m/%Y')} "
            f"— {DATA_MINIMA_HISTORICO.year} incluído."
        )
    else:
        dt_ini = ANO_ATUAL_INICIO.isoformat()
        if c2.button(
            f"📂 Carregar mais (incluir {DATA_MINIMA_HISTORICO.year})", key=f"{key}_mais"
        ):
            st.session_state[chave_2025] = True
            st.rerun()
    dt_fim = hoje.isoformat()
    return dt_ini, dt_fim


def seletor_contas(mapa_contas: dict, subs_codigos, key: str):
    """Multiselect de contas filtrado pelas subsidiárias selecionadas na
    sidebar, com lista de nomes completos abaixo (o multiselect trunca
    nomes longos nas 'pills')."""
    opcoes = q.contas_por_subsidiaria(mapa_contas, subs_codigos)
    # Chave inclui as subsidiárias selecionadas: ao mudar o filtro de
    # subsidiária, o widget "reseta" para a nova lista de opções em vez de
    # manter uma seleção antiga que pode conter contas fora do novo escopo.
    key_din = f"{key}_{'-'.join(subs_codigos) if subs_codigos else 'todas'}"
    sel = st.multiselect(
        "Contas", options=list(opcoes), format_func=lambda i: opcoes[i],
        default=list(opcoes), key=key_din,
    )
    if sel:
        with st.expander(f"📋 Nomes completos das {len(sel)} conta(s) selecionada(s)"):
            for cid in sel:
                st.caption(f"`{cid}` — {opcoes.get(cid, mapa_contas.get(cid, ''))}")
    return sel


# ---------------------------------------------------------------------------
# Sidebar: filtros globais + refresh
# ---------------------------------------------------------------------------

st.sidebar.title("🧮 Transitórias NetSuite")

if st.sidebar.button("🔄 Atualizar dados (limpar cache)", use_container_width=True):
    st.cache_data.clear()
    st.rerun()

try:
    df_subs = run_query(q.q_subsidiarias())
except NetSuiteError as exc:
    st.error(f"Falha de conexão com o NetSuite: {exc}")
    st.info("Confira o arquivo `.env` e as permissões do papel da integração (ver README).")
    st.stop()

# Mostra na sidebar SOMENTE as subsidiárias que de fato têm conta transitória
# mapeada no app (evita listar Controladora, Eliminação, UNIGOV etc., que só
# poluem o filtro e nunca batem com nenhuma conta).
df_subs["codigo"] = df_subs["name"].map(q.detectar_subsidiaria)
df_subs_rel = df_subs[df_subs["codigo"].notna()].copy()

subs_opts = dict(zip(df_subs_rel["name"], df_subs_rel["id"].astype(int)))
subs_cod_por_nome = dict(zip(df_subs_rel["name"], df_subs_rel["codigo"]))

subs_sel = st.sidebar.multiselect(
    "Subsidiárias", options=list(subs_opts), default=[],
    help="Vazio = todas as subsidiárias. Filtrar aqui também restringe "
         "automaticamente quais contas aparecem em cada aba.",
)
subs_ids = [subs_opts[s] for s in subs_sel] or None
subs_codes = [subs_cod_por_nome[s] for s in subs_sel] or None

st.sidebar.caption(
    f"Cache: {CACHE_TTL // 60} min · Agregações executadas no servidor (SuiteQL)."
)

tab_pag, tab_rec, tab_adq, tab_conc, tab_match = st.tabs(
    ["💳 Pagamentos", "📥 Recebimentos", "🏦 Adquirente", "⚖️ Conciliação", "🎯 Matching Avançado"]
)

# ===========================================================================
# ABA 1 — PAGAMENTOS
# ===========================================================================
with tab_pag:
    st.subheader("Transitórias de Pagamento — visão por DOC")
    st.caption(
        "Regra DOC validada: `CustPymt → tranid` · `Journal → Nº do pagamento "
        "(custcolcustcol_n_pagamento)`."
    )

    dt_ini_pag, dt_fim_pag = filtro_periodo("pag")
    st.warning(
        "⚠️ O CustPymt e o Journal de baixa de um mesmo DOC podem cair em "
        "datas diferentes (ex.: pagamento em 2025, baixa em 2026). Um DOC "
        f"que na verdade já zerou pode aparecer como divergente só por estar "
        f"fora da janela carregada — clique em 'Carregar mais (incluir "
        f"{DATA_MINIMA_HISTORICO.year})' antes de concluir que é uma "
        "exceção real."
    )

    contas_pag_sel = seletor_contas(q.CONTAS_PAGAMENTOS, subs_codes, "contas_pag")

    if contas_pag_sel:
        with st.spinner("Consultando KPIs no NetSuite..."):
            kpi = get_client().suiteql_scalar(
                q.q_pagamentos_kpi(contas_pag_sel, subs_ids, dt_ini_pag, dt_fim_pag)
            )
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Total de Lançamentos", f"{int(kpi.get('linhas', 0)):,}".replace(",", "."))
        c2.metric("Total Documentos Distintos", f"{int(kpi.get('docs', 0)):,}".replace(",", "."))
        c3.metric("Débitos", brl(float(kpi.get("total_debito") or 0)))
        c4.metric("Créditos", brl(float(kpi.get("total_credito") or 0)))
        c5.metric("Saldo (Déb − Créd)", brl(float(kpi.get("saldo") or 0)))

        with st.spinner("Consolidando por DOC (server-side)..."):
            df_doc = run_query(
                q.q_pagamentos_consolidado(
                    contas_pag_sel, subs_ids, True, dt_ini_pag, dt_fim_pag
                ),
                max_rows=None,
            )
        df_doc = num(df_doc, ["valor_debito", "valor_credito", "saldo", "qtd_linhas"])
        df_doc = df_doc.rename(columns={
            "doc": "Documento",
            "primeira_data": "Primeira Data",
            "qtd_linhas": "Qtd Linhas",
            "ultima_data": "Última Data",
            "valor_credito": "Valor Crédito",
            "valor_debito": "Valor Débito",
            "saldo": "Saldo",
        })
        ordem_cols = ["Documento", "Primeira Data", "Qtd Linhas", "Última Data",
                      "Valor Crédito", "Valor Débito", "Saldo"]
        df_doc = df_doc[[c for c in ordem_cols if c in df_doc.columns]]

        if not df_doc.empty:
            st.error(
                f"🔴 {len(df_doc)} DOC(s) não conciliado(s) · "
                f"Diferença total: {brl(df_doc['Saldo'].sum())}"
            )
        else:
            st.success("✅ Todos os DOCs zerados. Transitória de pagamento conciliada.")

        st.dataframe(
            estilizar(df_doc, moeda=["Valor Crédito", "Valor Débito", "Saldo"],
                      data=["Primeira Data", "Última Data"]).map(
                lambda v: f"color:{VERMELHO};font-weight:bold" if isinstance(v, (int, float)) and v != 0 else "",
                subset=["Saldo"] if "Saldo" in df_doc.columns else [],
            ),
            use_container_width=True, height=380,
        )
        if not df_doc.empty:
            botao_exportar(
                df_doc, "pagamentos_consolidado_doc", "pag_cons",
                moeda=["Valor Crédito", "Valor Débito", "Saldo"],
                data=["Primeira Data", "Última Data"],
            )

            st.markdown("##### 🔍 Drill-down por DOC")
            doc_sel = st.selectbox(
                "Selecione um DOC para abrir o razão detalhado",
                options=[""] + df_doc["Documento"].dropna().tolist(), key="doc_drill",
            )
            if doc_sel:
                df_det = run_query(q.q_pagamentos_detalhe_doc(contas_pag_sel, doc_sel))
                df_det = num(df_det, ["valor_debito", "valor_credito", "valor"])
                st.dataframe(
                    estilizar(df_det, moeda=["valor_debito", "valor_credito", "valor"],
                              data=["data", "a_partir_da_data", "data_criacao"]),
                    use_container_width=True,
                )
                st.caption(
                    f"Σ Débito {brl(df_det['valor_debito'].sum())} · "
                    f"Σ Crédito {brl(df_det['valor_credito'].sum())} · "
                    f"Saldo {brl(df_det['valor'].sum())}"
                )
                botao_exportar(
                    df_det, f"pagamentos_doc_{doc_sel}", "pag_det",
                    moeda=["valor_debito", "valor_credito", "valor"],
                    data=["data", "a_partir_da_data", "data_criacao"],
                )

# ===========================================================================
# ABA 2 — RECEBIMENTOS
# ===========================================================================
with tab_rec:
    st.subheader("Transitórias de Recebimento — Conta > Data")

    dt_ini_rec, dt_fim_rec = filtro_periodo("rec")
    contas_rec_sel = seletor_contas(q.CONTAS_RECEBIMENTOS, subs_codes, "contas_rec")

    if contas_rec_sel:
        with st.spinner("Consolidando Conta > Data..."):
            df_rec = run_query(
                q.q_recebimentos_consolidado(contas_rec_sel, dt_ini_rec, dt_fim_rec, subs_ids)
            )
        df_rec = num(df_rec, ["valor_debito", "valor_credito", "valor"])

        if df_rec.empty:
            st.info("Sem movimentação para os filtros selecionados.")
        else:
            for conta, grupo in df_rec.groupby("conta", sort=True):
                with st.expander(
                    f"**{conta}** — Déb {brl(grupo['valor_debito'].sum())} · "
                    f"Créd {brl(grupo['valor_credito'].sum())} · "
                    f"Saldo {brl(grupo['valor'].sum())}",
                    expanded=False,
                ):
                    st.dataframe(
                        estilizar(
                            grupo[["data", "valor_debito", "valor_credito", "valor"]],
                            moeda=["valor_debito", "valor_credito", "valor"], data=["data"],
                        ),
                        use_container_width=True, hide_index=True,
                    )
            botao_exportar(
                df_rec, "recebimentos_consolidado", "rec_cons",
                moeda=["valor_debito", "valor_credito", "valor"], data=["data"],
            )

            with st.expander("📄 Razão detalhado (linhas)"):
                if st.button("Carregar razão detalhado", key="btn_rec_det"):
                    df_rec_det = run_query(
                        q.q_recebimentos_detalhe(contas_rec_sel, dt_ini_rec, dt_fim_rec, subs_ids)
                    )
                    df_rec_det = num(df_rec_det, ["valor_debito", "valor_credito", "valor"])
                    st.dataframe(
                        estilizar(
                            df_rec_det, moeda=["valor_debito", "valor_credito", "valor"],
                            data=["data", "data_criacao", "data_recebimento"],
                        ),
                        use_container_width=True, height=420,
                    )
                    botao_exportar(
                        df_rec_det, "recebimentos_detalhe", "rec_det",
                        moeda=["valor_debito", "valor_credito", "valor"],
                        data=["data", "data_criacao", "data_recebimento"],
                    )

# ===========================================================================
# ABA 3 — ADQUIRENTE
# ===========================================================================
with tab_adq:
    st.subheader("Contas Adquirente — razão com parsing do memorando")
    st.caption(
        "Colunas TID/NSU/ARP/PARCELA/FATURA/COBRANÇA extraídas do memorando "
        "no servidor (REGEXP_SUBSTR, case-insensitive). Base completa: ~388 mil linhas."
    )

    dt_ini_adq, dt_fim_adq = filtro_periodo("adq")
    contas_adq_sel = seletor_contas(q.CONTAS_ADQUIRENTE, subs_codes, "contas_adq")

    if contas_adq_sel:
        with st.spinner("Consolidando adquirentes..."):
            df_adq = run_query(
                q.q_adquirente_consolidado(contas_adq_sel, dt_ini_adq, dt_fim_adq, subs_ids)
            )
        df_adq = num(df_adq, ["valor_debito", "valor_credito", "valor", "qtd_linhas"])

        if df_adq.empty:
            st.info("Sem movimentação para os filtros selecionados.")
        else:
            resumo_conta = (
                df_adq.groupby("conta", as_index=False)[
                    ["valor_debito", "valor_credito", "valor"]
                ].sum()
            )
            st.dataframe(
                estilizar(resumo_conta, moeda=["valor_debito", "valor_credito", "valor"]),
                use_container_width=True, hide_index=True,
            )
            fig = px.bar(
                df_adq, x="data", y="valor", color="conta",
                title="Movimentação líquida diária por conta adquirente",
            )
            st.plotly_chart(fig, use_container_width=True)
            botao_exportar(
                df_adq, "adquirente_consolidado", "adq_cons",
                moeda=["valor_debito", "valor_credito", "valor"], data=["data"],
            )

            with st.expander("📄 Razão detalhado com colunas calculadas"):
                if st.button("Carregar razão detalhado", key="btn_adq_det"):
                    prog = st.progress(0.0, "Baixando linhas do NetSuite...")

                    def _cb(n, total):
                        if total:
                            prog.progress(min(n / total, 1.0), f"{n:,} / {total:,} linhas")

                    df_adq_det = get_client().suiteql(
                        q.q_adquirente_detalhe(contas_adq_sel, dt_ini_adq, dt_fim_adq, subs_ids),
                        progress_cb=_cb,
                    )
                    prog.empty()
                    df_adq_det = num(df_adq_det, ["valor_debito", "valor_credito", "valor"])
                    st.dataframe(
                        estilizar(
                            df_adq_det, moeda=["valor_debito", "valor_credito", "valor"],
                            data=["data", "data_criacao", "data_recebimento"],
                        ),
                        use_container_width=True, height=420,
                    )
                    st.caption(
                        "Parsing: campos ausentes no memorando ficam vazios sem "
                        "quebrar as demais colunas (memos não estruturados existem na base)."
                    )
                    botao_exportar(
                        df_adq_det, "adquirente_detalhe", "adq_det",
                        moeda=["valor_debito", "valor_credito", "valor"],
                        data=["data", "data_criacao", "data_recebimento"],
                    )

# ===========================================================================
# ABA 4 — CONCILIAÇÃO (TID + NSU + ARP)
# ===========================================================================
with tab_conc:
    st.subheader("Conciliação por chave TID + NSU + ARP")

    ESCOPOS = {
        "Só Adquirente (saldo por chave)": "ADQ",
        "Adquirente × Pagamentos": "ADQ_PAG",
        "Adquirente × Recebimentos": "ADQ_REC",
    }
    escopo_lbl = st.radio("Escopo do cruzamento", list(ESCOPOS), horizontal=True)
    escopo = ESCOPOS[escopo_lbl]
    if escopo == "ADQ":
        st.caption("Regra: saldo (débito − crédito) da chave dentro da adquirente ≠ 0 → divergente. "
                   "Inclui parcelas a receber — use o prazo de liquidação para separar timing de erro.")
    else:
        st.caption("Regra de dupla partida: ADQ×PAG compara débitos ADQ vs débitos PAG (a venda entrou "
                   "nas duas pontas?); ADQ×REC compara créditos ADQ vs débitos REC (o saque saiu e entrou?).")

    dt_ini_cc, dt_fim_cc = filtro_periodo("cc")

    c4, c5 = st.columns(2)
    prazo_liq = c4.number_input(
        "Prazo normal de liquidação (dias)", 1, 365, 45,
        help="Chaves com saldo devedor dentro desse prazo são classificadas como "
             "'EM ABERTO (a receber)' — diferença de timing, não erro. "
             "Acima do prazo ou com saldo credor → 'INVESTIGAR'.",
    )
    max_diverg = c5.slider(
        "Máx. de divergências a carregar na tela", 1_000, 100_000, 20_000, step=1_000,
        help="Ordenadas por |valor| decrescente. Use a exportação para a lista completa.",
    )

    if st.button("▶️ Executar conciliação", type="primary", key="btn_conc"):
        with st.spinner("Agregando por chave no NetSuite (server-side)..."):
            resumo = get_client().suiteql_scalar(
                q.q_conciliacao_resumo(escopo, dt_ini_cc, dt_fim_cc, subs_ids)
            )
        total = int(resumo.get("total_chaves") or 0)
        ok = int(resumo.get("chaves_ok") or 0)
        div = int(resumo.get("chaves_divergentes") or 0)
        val = float(resumo.get("valor_divergente") or 0)
        pct = (ok / total * 100) if total else 0.0

        c1, c2, c3, c4b = st.columns(4)
        c1.metric("Total de chaves", f"{total:,}".replace(",", "."))
        c2.metric("% conciliado", f"{pct:.2f}%")
        c3.metric("Chaves divergentes", f"{div:,}".replace(",", "."))
        c4b.metric("Valor divergente", brl(val))

        if div == 0:
            st.success("✅ 100% conciliado no escopo selecionado.")
        else:
            prog = st.progress(0.0, "Baixando divergências...")

            def _cb(n, tot):
                if tot:
                    prog.progress(min(n / min(tot, max_diverg), 1.0),
                                  f"{n:,} divergências baixadas")

            df_div = get_client().suiteql(
                q.q_conciliacao_divergencias(escopo, dt_ini_cc, dt_fim_cc, subs_ids),
                max_rows=max_diverg, progress_cb=_cb,
            )
            prog.empty()
            df_div = num(df_div, ["diferenca", "deb_adq", "cred_adq", "deb_contraparte",
                                  "cred_contraparte", "qtd_lancamentos", "dias_desde_ultimo"])
            # Ordena por maior diferença aqui (pandas), não no SQL: ordenar
            # por uma expressão agregada (ABS(diferenca)) direto no NetSuite
            # falha silenciosamente quando o GROUP BY é muito grande (ver
            # q_pagamentos_consolidado) — o SQL busca em ordem de chave
            # (estável) e o maior-primeiro é aplicado só depois de baixar.
            df_div = df_div.sort_values(by="diferenca", key=lambda s: s.abs(), ascending=False)
            if len(df_div) >= max_diverg:
                st.warning(
                    f"⚠️ Atingido o limite de {max_diverg:,} linhas baixadas — pode "
                    "haver mais chaves divergentes além destas. Como a busca é feita "
                    "em ordem de chave (não por tamanho da diferença), as maiores "
                    "divergências podem não estar todas nesta amostra. Reduza o "
                    "período/escopo para garantir a lista completa."
                    .replace(",", ".")
                )

            # ------------------------------------------------------------------
            # Classificação com aging (metodologia de conciliação):
            #   0-30 atual · 31-60 em acompanhamento · 61-90 vencido · 90+ crítico
            # Saldo devedor dentro do prazo de liquidação = timing (a receber).
            # ------------------------------------------------------------------
            def classifica(r):
                dias = r["dias_desde_ultimo"] or 0
                if r["diferenca"] > 0 and dias <= prazo_liq:
                    return "🟡 EM ABERTO (a receber)"
                return "🔴 INVESTIGAR"

            def bucket(d):
                d = d or 0
                if d <= 30: return "0-30 dias"
                if d <= 60: return "31-60 dias"
                if d <= 90: return "61-90 dias"
                return "90+ dias"

            df_div["status"] = df_div.apply(classifica, axis=1)
            df_div["aging"] = df_div["dias_desde_ultimo"].map(bucket)

            n_inv = (df_div["status"] == "🔴 INVESTIGAR").sum()
            v_inv = df_div.loc[df_div["status"] == "🔴 INVESTIGAR", "diferenca"].sum()
            st.error(
                f"🔴 {n_inv:,} chave(s) para INVESTIGAR "
                f"({brl(v_inv)}) — fora do prazo de liquidação ou saldo credor."
                .replace(",", ".")
            )

            colA, colB = st.columns(2)
            ag = (df_div.groupby(["aging", "status"], as_index=False)
                  .agg(qtd=("chave", "count"), valor=("diferenca", "sum")))
            figa = px.bar(
                ag, x="aging", y="valor", color="status", barmode="stack",
                category_orders={"aging": ["0-30 dias", "31-60 dias", "61-90 dias", "90+ dias"]},
                color_discrete_map={"🟡 EM ABERTO (a receber)": "#f9a825",
                                    "🔴 INVESTIGAR": VERMELHO},
                title="Divergências por aging (R$)",
            )
            colA.plotly_chart(figa, use_container_width=True)
            figb = px.bar(
                ag, x="aging", y="qtd", color="status", barmode="stack",
                category_orders={"aging": ["0-30 dias", "31-60 dias", "61-90 dias", "90+ dias"]},
                color_discrete_map={"🟡 EM ABERTO (a receber)": "#f9a825",
                                    "🔴 INVESTIGAR": VERMELHO},
                title="Divergências por aging (quantidade)",
            )
            colB.plotly_chart(figb, use_container_width=True)

            filtro_status = st.multiselect(
                "Filtrar status", ["🔴 INVESTIGAR", "🟡 EM ABERTO (a receber)"],
                default=["🔴 INVESTIGAR"],
            )
            df_show = df_div[df_div["status"].isin(filtro_status)] if filtro_status else df_div
            cols_conc = [c for c in ["status", "aging", "chave", "diferenca",
                         "deb_adq", "cred_adq", "deb_contraparte", "cred_contraparte", "qtd_lancamentos",
                         "origens", "primeira_data", "ultima_data",
                         "dias_desde_ultimo", "subsidiarias", "contas"]
                         if c in df_show.columns]
            st.dataframe(
                estilizar(
                    df_show[cols_conc],
                    moeda=["diferenca", "deb_adq", "cred_adq", "deb_contraparte", "cred_contraparte"],
                    data=["primeira_data", "ultima_data"],
                ),
                use_container_width=True, height=420,
            )
            botao_exportar(
                df_div, f"conciliacao_divergencias_{escopo}", "conc_div",
                moeda=["diferenca", "deb_adq", "cred_adq", "deb_contraparte", "cred_contraparte"],
                data=["primeira_data", "ultima_data"],
            )

            st.markdown("##### 🔍 Drill-down por chave")
            st.caption("Cole o TID (primeiro segmento da chave) para abrir todos os lançamentos.")
            tid_drill = st.text_input("TID", key="tid_drill")
            if tid_drill.strip():
                df_ch = run_query(q.q_conciliacao_detalhe_chave(tid_drill.strip()))
                df_ch = num(df_ch, ["valor_debito", "valor_credito", "valor"])
                st.dataframe(
                    estilizar(df_ch, moeda=["valor_debito", "valor_credito", "valor"], data=["data"]),
                    use_container_width=True,
                )
                if not df_ch.empty:
                    st.caption(f"Saldo da chave: {brl(df_ch['valor'].sum())}")
                    botao_exportar(
                        df_ch, f"chave_{tid_drill.strip()}", "conc_chave",
                        moeda=["valor_debito", "valor_credito", "valor"], data=["data"],
                    )

st.sidebar.divider()
st.sidebar.caption(
    "⚠️ Ferramenta de apoio à conciliação — os resultados devem ser revisados "
    "pela contabilidade antes do fechamento."
)

# ===========================================================================
# ABA 5 — MATCHING AVANÇADO (waterfall multi-nível, linha a linha)
# ===========================================================================
with tab_match:
    st.subheader("Matching Avançado — cascata de prioridade com classificação de confiança")
    st.caption(
        "Compara cada LANÇAMENTO (não a chave agregada) entre Adquirente e a "
        "contraparte escolhida, testando critérios em ordem de prioridade "
        "(ID da Transação → TID/NSU/ARP/Fatura → combinações → busca em texto "
        "→ fallback aproximado). Ver `matching.py` para a especificação completa "
        "e o mapeamento de campos validado contra a base."
    )
    with st.expander("ℹ️ Mapeamento de campos usado (revisar se não bater com o esperado)"):
        st.markdown(
            "- **TID/NSU/ARP** — Pagamentos: campos de corpo `custbody_nscs_tid/nsu/arp`. "
            "Adquirente/Recebimentos: extraídos do memorando.\n"
            "- **Fatura** — memorando, com fallback para `custcolcustcol_id_fatura` / "
            "`custbody_nscs_faturavindi`.\n"
            "- **Parcela** — extraída do memorando nas três origens.\n"
            "- **ID da Transação** — ADQ×PAG: `custcolcustcol_n_pagamento` (linha da "
            "Adquirente) comparado ao **tranid** do CustPymt (validado com dados reais). "
            "ADQ×REC: `custcolcustcoldata_idsaque` (nº do saque) — **N vendas podem "
            "apontar para o mesmo saque/depósito**, tratado corretamente pelo motor.\n"
            "- **Valor** — ADQ×PAG: débito × débito. ADQ×REC: crédito (Adquirente) × "
            "débito (Recebimento).\n\n"
            "⚠️ Testamos também o campo *'Transação NNNNNN'* que aparece em alguns "
            "formatos de memorando (possível ID do gateway Vindi), mas ele **não** "
            "aparece de forma consistente nos dois lados para a mesma venda — por "
            "isso não foi usado como 'ID da Transação'. Se o time entender esse "
            "conceito de forma diferente, este é o ponto a ajustar."
        )

    ESCOPOS_MATCH = {
        "Adquirente × Pagamentos": "PAG",
        "Adquirente × Recebimentos": "REC",
    }
    c1, c2 = st.columns([2, 1])
    escopo_lbl_m = c1.radio("Escopo do matching", list(ESCOPOS_MATCH), horizontal=True, key="escopo_match")
    escopo_m = ESCOPOS_MATCH[escopo_lbl_m]

    c1, c2, c3 = st.columns(3)
    dt_ini_m = c1.date_input(
        "Data início", hoje - timedelta(days=7), min_value=DATA_MINIMA_HISTORICO, key="di_match"
    ).isoformat()
    dt_fim_m = c2.date_input(
        "Data fim", hoje, min_value=DATA_MINIMA_HISTORICO, key="df_match"
    ).isoformat()
    c3.caption(
        "⚠️ Período **obrigatório** (sem opção 'todo o período'): o matching é "
        "linha a linha, não agregado — comece com poucos dias e amplie conforme "
        "o desempenho permitir."
    )

    with st.expander("⚙️ Opções avançadas (desempenho)"):
        c1, c2 = st.columns(2)
        habilitar_texto = c1.toggle("Habilitar busca em texto (Nível 8)", value=True, key="hab_texto")
        habilitar_aprox = c1.toggle("Habilitar fallback aproximado (Nível 9)", value=True, key="hab_aprox")
        limite_similaridade = c2.slider(
            "Limite p/ similaridade textual (pares)", 1_000, 50_000, 10_000, step=1_000,
            help="Etapa mais lenta (compara texto par a par). Acima do limite, essa "
                 "etapa específica é pulada — as demais continuam normalmente.",
        )

    if st.button("▶️ Executar matching", type="primary", key="btn_match"):
        contas_pag_m = q.contas_por_subsidiaria(q.CONTAS_PAGAMENTOS, subs_codes)
        contas_rec_m = q.contas_por_subsidiaria(q.CONTAS_RECEBIMENTOS, subs_codes)
        contas_adq_m = q.contas_por_subsidiaria(q.CONTAS_ADQUIRENTE, subs_codes)

        with st.spinner("Baixando linhas do NetSuite (Adquirente + contraparte)..."):
            df_adq_raw = run_query(q.q_adquirente_matching(contas_adq_m, dt_ini_m, dt_fim_m, subs_ids))
            if escopo_m == "PAG":
                df_ctp_raw = run_query(q.q_pagamentos_matching(contas_pag_m, dt_ini_m, dt_fim_m, subs_ids))
            else:
                df_ctp_raw = run_query(q.q_recebimentos_matching(contas_rec_m, dt_ini_m, dt_fim_m, subs_ids))

        if df_adq_raw.empty or df_ctp_raw.empty:
            st.session_state.pop("match_resultado", None)
            st.info("Sem linhas para os filtros selecionados em um dos dois lados.")
        else:
            # Normaliza o lado Adquirente conforme o escopo: campo de valor e
            # de "id_transacao" mudam (débito×débito em PAG; crédito(ADQ)×débito(REC)).
            df_adq_raw = df_adq_raw.copy()
            df_adq_raw["valor"] = pd.to_numeric(
                df_adq_raw["valor_debito" if escopo_m == "PAG" else "valor_credito"], errors="coerce"
            )
            df_adq_raw["id_transacao"] = df_adq_raw[
                "id_transacao_pag" if escopo_m == "PAG" else "id_transacao_rec"
            ]

            df_adq_m = mt.preparar(df_adq_raw, "ADQ")
            df_ctp_m = mt.preparar(df_ctp_raw, "CTP")

            prog = st.progress(0.0, "Executando cascata de prioridade...")

            def _cb(nome, n_pares, resta_adq, resta_ctp):
                prog.progress(0.5, f"{nome}: +{n_pares} par(es) · restam {resta_adq}×{resta_ctp}")

            pareados, sobra_adq, sobra_ctp, info = mt.waterfall_match(
                df_adq_m, df_ctp_m, escopo=escopo_m,
                habilitar_texto=habilitar_texto,
                habilitar_aproximado=habilitar_aprox,
                limite_pares_similaridade=limite_similaridade,
                progress_cb=_cb,
            )
            prog.empty()

            # Guarda os resultados no session_state: assim a exibição abaixo
            # (fora deste "if") sobrevive a reruns disparados pelo slider de
            # confiança ou pelos botões de exportação — sem isso, qualquer
            # interação com esses widgets fazia toda a análise desaparecer,
            # pois o clique do botão "Executar matching" só vale para o run
            # em que ele foi de fato clicado.
            # ATENÇÃO: uma nova execução reinicia os matches manuais desta
            # sessão (o conjunto de linhas "sem correspondência" é
            # recalculado do zero).
            st.session_state["match_resultado"] = {
                "pareados": pareados, "sobra_adq": sobra_adq, "sobra_ctp": sobra_ctp,
                "manuais": pd.DataFrame(), "info": info,
                "escopo_m": escopo_m, "escopo_lbl_m": escopo_lbl_m,
                "total_adq": len(df_adq_m), "valor_total_adq": df_adq_m["valor"].sum(),
            }

    # ---- Exibição dos resultados (lê do session_state, não do "if" acima) ----
    if "match_resultado" in st.session_state:
        res = st.session_state["match_resultado"]
        pareados, sobra_adq, sobra_ctp = res["pareados"], res["sobra_adq"], res["sobra_ctp"]
        manuais = res.get("manuais", pd.DataFrame())
        info, escopo_m, escopo_lbl_m = res["info"], res["escopo_m"], res["escopo_lbl_m"]
        total_adq, valor_total_adq = res["total_adq"], res["valor_total_adq"]
        rotulo_ctp = escopo_lbl_m.split("×")[1].strip()

        if info.get("texto_pulado"):
            st.warning(
                "⚠️ Busca em texto (Nível 8) foi **pulada**: volume remanescente "
                "grande demais para essa etapa. Reduza o período para incluí-la."
            )
        if info.get("similaridade_pulada"):
            st.warning(
                "⚠️ Similaridade textual foi **pulada**: volume remanescente acima "
                "do limite configurado em 'Opções avançadas'. Aumente o limite ou "
                "reduza o período."
            )

        # Automáticos + manuais combinados — é o que conta para KPIs, gráficos,
        # tabela e exportação a partir daqui.
        todos_pareados = (
            pd.concat([pareados, manuais], ignore_index=True, sort=False)
            if not manuais.empty else pareados
        )

        n_pareados = len(todos_pareados)
        valor_pareado = todos_pareados["valor_adq"].sum() if not todos_pareados.empty else 0.0

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Linhas Adquirente", f"{total_adq:,}".replace(",", "."))
        c2.metric(
            "% pareado (Adquirente)",
            f"{(n_pareados/total_adq*100 if total_adq else 0):.1f}%",
        )
        c3.metric("Sem match (ADQ)", f"{len(sobra_adq):,}".replace(",", "."))
        c4.metric("Valor pareado", f"{brl(valor_pareado)} / {brl(valor_total_adq)}")
        if not manuais.empty:
            st.caption(f"↳ inclui {len(manuais)} match(es) manual(is) registrado(s) nesta sessão.")

        if not todos_pareados.empty:
            st.markdown("##### Distribuição por nível e confiança")
            resumo_nivel = (
                todos_pareados.groupby(["nivel", "regra", "confianca"], as_index=False)
                .agg(qtd=("_id_adq", "count"), valor=("valor_adq", "sum"))
                .sort_values(["nivel", "confianca"], ascending=[True, False])
            )
            colA, colB = st.columns(2)
            fig1 = px.bar(
                resumo_nivel, x="nivel", y="qtd", color="confianca",
                hover_data=["regra"], title="Pares por nível (quantidade)",
                color_continuous_scale="RdYlGn",
            )
            colA.plotly_chart(fig1, use_container_width=True)

            faixa = pd.cut(
                todos_pareados["confianca"], bins=[0, 60, 80, 90, 95, 100],
                labels=["≤60% Baixa", "61-80% Média", "81-90% Alta", "91-95% Alta+", "96-100% Muito Alta"],
                include_lowest=True,
            )
            dist_conf = faixa.value_counts().sort_index()
            fig2 = px.pie(
                values=dist_conf.values, names=dist_conf.index,
                title="Distribuição por faixa de confiança",
            )
            colB.plotly_chart(fig2, use_container_width=True)

            st.dataframe(estilizar(resumo_nivel, moeda=["valor"]), use_container_width=True, hide_index=True)

            st.markdown("##### Pares encontrados")
            faixa_min = st.slider("Confiança mínima a exibir", 0, 100, 0, step=5, key="faixa_min_match")
            df_show = todos_pareados[todos_pareados["confianca"] >= faixa_min]
            cols_exibir = [c for c in [
                "codigo_conciliacao", "manual", "nivel", "regra", "confianca",
                "numero_documento_adq", "numero_documento_ctp",
                "valor_adq", "valor_ctp", "data_adq", "data_ctp", "_dif_valor", "_dif_dias",
                "_score_similaridade", "observacao",
            ] if c in df_show.columns]
            st.dataframe(
                estilizar(
                    df_show[cols_exibir], moeda=["valor_adq", "valor_ctp", "_dif_valor"],
                    data=["data_adq", "data_ctp"],
                ),
                use_container_width=True, height=380,
            )
            botao_exportar(
                todos_pareados, f"matching_{escopo_m}_pareados", "match_par",
                moeda=["valor_adq", "valor_ctp", "_dif_valor"], data=["data_adq", "data_ctp"],
            )
        else:
            st.info("Nenhum par encontrado com os filtros e opções selecionadas.")

        # -------------------------------------------------------------------
        # Match manual — registrar par que o motor automático não encontrou
        # -------------------------------------------------------------------
        st.markdown("##### 🔗 Registrar match manual")
        with st.expander(
            f"Selecionar uma linha sem correspondência de cada lado e confirmar "
            f"({len(sobra_adq)} Adquirente × {len(sobra_ctp)} {rotulo_ctp} disponíveis)"
        ):
            if sobra_adq.empty or sobra_ctp.empty:
                st.caption(
                    "Não há linhas sem correspondência dos dois lados simultaneamente "
                    "— nada para parear manualmente."
                )
            else:
                def _rotulo(row):
                    d = row["data"].date() if pd.notna(row["data"]) else "?"
                    memo_curto = str(row["memo"])[:40] if pd.notna(row["memo"]) else ""
                    return f"{row['numero_documento']} · {brl(row['valor'])} · {d} · {memo_curto}"

                opcoes_adq = {row["_id"]: _rotulo(row) for _, row in sobra_adq.iterrows()}
                opcoes_ctp = {row["_id"]: _rotulo(row) for _, row in sobra_ctp.iterrows()}

                c1, c2 = st.columns(2)
                id_adq_sel = c1.selectbox(
                    "Linha do Adquirente", options=list(opcoes_adq),
                    format_func=lambda i: opcoes_adq[i], key="manual_sel_adq",
                )
                id_ctp_sel = c2.selectbox(
                    f"Linha de {rotulo_ctp}", options=list(opcoes_ctp),
                    format_func=lambda i: opcoes_ctp[i], key="manual_sel_ctp",
                )

                row_adq_sel = sobra_adq[sobra_adq["_id"] == id_adq_sel].iloc[0]
                row_ctp_sel = sobra_ctp[sobra_ctp["_id"] == id_ctp_sel].iloc[0]

                dif_valor = (
                    abs(row_adq_sel["valor"] - row_ctp_sel["valor"])
                    if pd.notna(row_adq_sel["valor"]) and pd.notna(row_ctp_sel["valor"]) else None
                )
                dif_dias = (
                    abs((row_adq_sel["data"] - row_ctp_sel["data"]).days)
                    if pd.notna(row_adq_sel["data"]) and pd.notna(row_ctp_sel["data"]) else None
                )

                cA, cB = st.columns(2)
                cA.metric("Valor Adquirente", brl(row_adq_sel["valor"]))
                cB.metric(f"Valor {rotulo_ctp}", brl(row_ctp_sel["valor"]))
                if dif_valor is not None and dif_valor > 0.01:
                    st.warning(
                        f"⚠️ Os valores diferem em {brl(dif_valor)}. O match manual "
                        f"não é bloqueado por isso — confirme que é o mesmo "
                        f"lançamento antes de prosseguir."
                    )
                if dif_dias is not None and dif_dias > 5:
                    st.warning(f"⚠️ As datas diferem em {dif_dias} dia(s).")

                observacao_m = st.text_input(
                    "Observação (opcional — ex.: motivo do match manual)", key="manual_obs"
                )

                if st.button("✅ Confirmar match manual", key="btn_confirmar_manual"):
                    par = mt.construir_par_manual(row_adq_sel, row_ctp_sel, escopo_m, observacao_m)
                    st.session_state["match_resultado"]["manuais"] = pd.concat(
                        [manuais, pd.DataFrame([par])], ignore_index=True, sort=False
                    )
                    st.session_state["match_resultado"]["sobra_adq"] = sobra_adq[
                        sobra_adq["_id"] != id_adq_sel
                    ]
                    st.session_state["match_resultado"]["sobra_ctp"] = sobra_ctp[
                        sobra_ctp["_id"] != id_ctp_sel
                    ]
                    st.success(f"Match manual registrado: `{par['codigo_conciliacao']}`")
                    st.rerun()

        if not manuais.empty:
            st.markdown("###### Matches manuais confirmados nesta sessão")
            for idx, row in manuais.iterrows():
                cline, cbtn = st.columns([5, 1])
                obs_txt = f" · _{row['observacao']}_" if row.get("observacao") else ""
                cline.markdown(
                    f"`{row['codigo_conciliacao']}` — **{row['numero_documento_adq']}** ↔ "
                    f"**{row['numero_documento_ctp']}** · {brl(row['valor_adq'])} / "
                    f"{brl(row['valor_ctp'])}{obs_txt}"
                )
                if cbtn.button("↩️ Desfazer", key=f"desfazer_manual_{idx}"):
                    linha_adq = {
                        col[: -len("_adq")]: row[col]
                        for col in manuais.columns if col.endswith("_adq")
                    }
                    linha_ctp = {
                        col[: -len("_ctp")]: row[col]
                        for col in manuais.columns if col.endswith("_ctp")
                    }
                    st.session_state["match_resultado"]["manuais"] = manuais.drop(idx)
                    st.session_state["match_resultado"]["sobra_adq"] = pd.concat(
                        [sobra_adq, pd.DataFrame([linha_adq])], ignore_index=True, sort=False
                    )
                    st.session_state["match_resultado"]["sobra_ctp"] = pd.concat(
                        [sobra_ctp, pd.DataFrame([linha_ctp])], ignore_index=True, sort=False
                    )
                    st.rerun()

        st.markdown("##### ⚠️ Sem correspondência (para investigação)")
        colX, colY = st.columns(2)
        with colX:
            st.caption(f"Adquirente — {len(sobra_adq)} linha(s) sem par")
            cols_sobra = [c for c in ["numero_documento", "valor", "data", "tid", "nsu", "arp", "fatura", "memo"] if c in sobra_adq.columns]
            st.dataframe(
                estilizar(sobra_adq[cols_sobra], moeda=["valor"], data=["data"]),
                use_container_width=True, height=300,
            )
            if not sobra_adq.empty:
                botao_exportar(
                    sobra_adq, f"matching_{escopo_m}_sobra_adquirente", "match_sadq",
                    moeda=["valor"], data=["data"],
                )
        with colY:
            st.caption(f"{rotulo_ctp} — {len(sobra_ctp)} linha(s) sem par")
            cols_sobra_c = [c for c in ["numero_documento", "valor", "data", "tid", "nsu", "arp", "fatura", "memo"] if c in sobra_ctp.columns]
            st.dataframe(
                estilizar(sobra_ctp[cols_sobra_c], moeda=["valor"], data=["data"]),
                use_container_width=True, height=300,
            )
            if not sobra_ctp.empty:
                botao_exportar(
                    sobra_ctp, f"matching_{escopo_m}_sobra_contraparte", "match_sctp",
                    moeda=["valor"], data=["data"],
                )
