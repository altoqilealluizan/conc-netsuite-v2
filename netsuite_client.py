"""
netsuite_client.py
==================
Cliente REST SuiteQL do NetSuite com autenticação TBA (OAuth 1.0a / HMAC-SHA256),
paginação automática por offset e retry com backoff exponencial para HTTP 429.

Endpoint utilizado:
    POST https://{account}.suitetalk.api.netsuite.com/services/rest/query/v1/suiteql

Limitações conhecidas da API REST SuiteQL:
    - Máximo de 1.000 linhas por página (parâmetro limit).
    - Máximo de 100.000 linhas por consulta (offset máximo). Para bases maiores,
      particione a consulta por período/subsidiária ou agregue no servidor.
    - CTEs (WITH) não são suportadas; use subqueries inline.
"""

from __future__ import annotations

import time
import logging
from typing import Callable, Optional

import pandas as pd
import requests
from requests_oauthlib import OAuth1

logger = logging.getLogger(__name__)

MAX_PAGE_SIZE = 1000          # limite da API por página
MAX_OFFSET = 100_000          # limite da API por consulta
MAX_RETRIES = 5               # tentativas em caso de 429/erros transitórios


class NetSuiteError(RuntimeError):
    """Erro de comunicação ou de consulta SuiteQL."""


class NetSuiteClient:
    """Cliente mínimo e reutilizável para SuiteQL via REST."""

    def __init__(
        self,
        account: str,
        consumer_key: str,
        consumer_secret: str,
        token_id: str,
        token_secret: str,
        timeout: int = 180,
    ) -> None:
        if not all([account, consumer_key, consumer_secret, token_id, token_secret]):
            raise NetSuiteError(
                "Credenciais incompletas. Verifique o arquivo .env "
                "(NS_ACCOUNT, NS_CONSUMER_KEY, NS_CONSUMER_SECRET, NS_TOKEN_ID, NS_TOKEN_SECRET)."
            )

        # URL usa hífen/minúsculas (contas sandbox: 9339456-sb1 -> 9339456-sb1)
        url_account = account.strip().lower().replace("_", "-")
        # O realm do OAuth usa maiúsculas e underscore (sandbox: 9339456_SB1)
        self.realm = account.strip().upper().replace("-", "_")

        self.base_url = (
            f"https://{url_account}.suitetalk.api.netsuite.com"
            "/services/rest/query/v1/suiteql"
        )
        self.timeout = timeout
        self.auth = OAuth1(
            client_key=consumer_key,
            client_secret=consumer_secret,
            resource_owner_key=token_id,
            resource_owner_secret=token_secret,
            signature_method="HMAC-SHA256",
            realm=self.realm,
        )
        self.session = requests.Session()

    # ------------------------------------------------------------------
    def _request_page(self, sql: str, limit: int, offset: int) -> dict:
        """Executa uma página da consulta com retry/backoff para 429 e 5xx."""
        last_exc: Optional[Exception] = None
        for attempt in range(MAX_RETRIES):
            try:
                resp = self.session.post(
                    self.base_url,
                    params={"limit": limit, "offset": offset},
                    json={"q": sql},
                    headers={"Prefer": "transient"},
                    auth=self.auth,
                    timeout=self.timeout,
                )
                if resp.status_code == 429 or resp.status_code >= 500:
                    wait = 2 ** attempt
                    logger.warning(
                        "HTTP %s do NetSuite (tentativa %s/%s). Aguardando %ss...",
                        resp.status_code, attempt + 1, MAX_RETRIES, wait,
                    )
                    time.sleep(wait)
                    continue
                if resp.status_code >= 400:
                    # Erros de SuiteQL vêm com detalhe no corpo (o-errorDetails)
                    raise NetSuiteError(
                        f"Erro {resp.status_code} na consulta SuiteQL: {resp.text[:2000]}"
                    )
                return resp.json()
            except requests.RequestException as exc:  # timeout, conexão etc.
                last_exc = exc
                time.sleep(2 ** attempt)
        raise NetSuiteError(f"Falha após {MAX_RETRIES} tentativas: {last_exc}")

    # ------------------------------------------------------------------
    def suiteql(
        self,
        sql: str,
        max_rows: Optional[int] = None,
        progress_cb: Optional[Callable[[int, Optional[int]], None]] = None,
    ) -> pd.DataFrame:
        """
        Executa uma consulta SuiteQL paginando até o fim (ou até max_rows).

        Parameters
        ----------
        sql : str
            Consulta SuiteQL (sem ponto e vírgula final).
        max_rows : int, opcional
            Corta a extração ao atingir esse número de linhas (proteção de UI).
        progress_cb : callable(rows_baixadas, total_estimado), opcional
            Callback para barra de progresso no Streamlit.
        """
        rows: list[dict] = []
        offset = 0
        while True:
            page_limit = MAX_PAGE_SIZE
            if max_rows is not None:
                page_limit = min(page_limit, max_rows - len(rows))
                if page_limit <= 0:
                    break

            data = self._request_page(sql, page_limit, offset)
            items = data.get("items", [])
            rows.extend(items)

            if progress_cb:
                progress_cb(len(rows), data.get("totalResults"))

            # IMPORTANTE: o offset da próxima página deve ser sempre um
            # múltiplo do "page_limit" que foi de fato enviado nesta página
            # (exigência da API REST). Para consultas com GROUP BY/HAVING o
            # campo "hasMore" pode ficar inconsistente com a contagem
            # pós-agregação (uma página "curta" ainda reportando hasMore=True),
            # o que faria o offset avançar por um valor não-múltiplo de
            # page_limit e a API rejeitar a página seguinte com "Invalid
            # limit and offset values". Por isso o critério de parada aqui é
            # "voltou menos itens do que o solicitado", não o hasMore.
            if not items or len(items) < page_limit:
                break
            offset += page_limit
            if offset >= MAX_OFFSET:
                logger.warning(
                    "Limite de 100.000 linhas da API REST atingido; "
                    "resultado truncado. Refine os filtros da consulta."
                )
                break

        df = pd.DataFrame(rows)
        # A API devolve uma coluna 'links' de HATEOAS sem valor analítico
        if "links" in df.columns:
            df = df.drop(columns=["links"])
        return df

    # ------------------------------------------------------------------
    def suiteql_scalar(self, sql: str) -> dict:
        """Executa consulta que retorna 1 linha (KPIs) e devolve o dict."""
        df = self.suiteql(sql, max_rows=1)
        return {} if df.empty else df.iloc[0].to_dict()
