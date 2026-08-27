# 🧮 Conciliação de Contas Transitórias — NetSuite

Dashboard Streamlit para extração e conciliação das contas transitórias de
**pagamento**, **recebimento** e **adquirente** de cartão, conectado diretamente
ao NetSuite via **SuiteQL REST** (Token-Based Authentication).

## 🆕 Changelog desta versão

**Revertida a fusão Conciliação + Matching Avançado**
Uma versão anterior desta sessão havia unificado as abas "⚖️ Conciliação"
e "🎯 Matching Avançado" em um funil de 2 camadas (triagem agregada →
investigação linha a linha com botão "Investigar em detalhe"). A pedido
da usuária, essa fusão foi **desfeita** — o app voltou a ter as duas como
abas separadas (5 abas), preservando todas as demais melhorias feitas
depois da fusão original (match manual, código de conciliação estável,
formatação R$/dd-mm-aaaa, filtro por ano 2026/2025). Se quiser revisitar
a ideia do funil integrado no futuro, a explicação da proposta e as duas
opções discutidas continuam válidas.

**Carregamento por período: 2026 por padrão, "Carregar mais" para 2025**
(decisão de negócio 2026-07-16) Pagamentos, Recebimentos, Adquirente e
Conciliação agora carregam por padrão só o ano corrente (01/01/2026 até
hoje) — mais rápido, e cobre o que normalmente importa. Um botão
**"📂 Carregar mais (incluir 2025)"** expande a busca para incluir também
2025. O histórico anterior a 01/01/2025 nunca é buscado — nem no modo
"Período personalizado", cujo seletor de data não permite escolher algo
mais antigo. A aba Matching Avançado (que já exigia um período explícito)
também não deixa mais escolher datas antes de 01/01/2025.
⚠️ Em Pagamentos, isso interage com o aviso já existente sobre DOCs cujo
CustPymt e Journal de baixa caem em anos diferentes — o app aponta o botão
"Carregar mais" como o primeiro passo antes de tratar algo como divergência
real.

**Formatação: valores em Real (R$ 0.000,00) e datas em dd/mm/aaaa**
Todas as tabelas do app (as 5 abas) agora exibem colunas de valor no padrão
`R$ 0.000,00` e colunas de data no padrão `dd/mm/aaaa`:
- **Na tela**: via `pandas.Styler.format()` — muda só a exibição, os valores
  continuam numéricos/data por baixo (soma, ordenação e o destaque em
  vermelho de DOCs não zerados continuam funcionando normalmente).
- **No Excel exportado**: formato NATIVO do Excel (`R$ #.##0,00` e
  `dd/mm/aaaa`) — os valores continuam numéricos/data de verdade na
  planilha, dá para somar e filtrar no Excel normalmente, só muda como a
  célula é exibida.
- **No CSV exportado**: moeda já saía com `decimal=","` (padrão BR); datas
  agora são convertidas para o texto `dd/mm/aaaa` antes de exportar (CSV
  não tem formato nativo de célula).

**Bug corrigido: tela vazia em Pagamentos com "Somente DOCs não zerados" desligado.**
Descoberta uma limitação real do NetSuite: `ORDER BY` numa coluna **agregada**
(ex.: `saldo`, um `SUM`) faz a API REST retornar **0 linhas silenciosamente**
quando o `GROUP BY` tem muitos grupos (validado com ~125 mil DOCs distintos
em Pagamentos — a paginação segue dizendo que há mais páginas, mas o array
de dados vem vazio). Ordenar pela própria chave do agrupamento (não-agregada)
funciona normalmente em qualquer escala. Corrigido em duas consultas:
- `q_pagamentos_consolidado`: com o toggle "Somente DOCs não zerados" ligado
  (poucas linhas, pós-filtro), continua ordenando por saldo. Desligado (até
  ~125 mil DOCs), passa a ordenar pelo DOC.
- `q_conciliacao_divergencias`: mesma limitação valeria para o escopo "Só
  Adquirente" sem filtro de data (~130 mil chaves divergentes, mesma ordem
  de grandeza) — corrigido preventivamente antes de dar problema. A
  ordenação por "maior diferença primeiro" que aparece na tela agora é
  feita em pandas, depois do download (não mais no SQL). Se o volume
  baixado atingir o limite configurado, o app avisa que pode haver mais
  chaves divergentes além da amostra (já que a busca passou a ser em ordem
  de chave, não por tamanho da diferença).

**Novo: Match Manual + Código de Conciliação (🎯 Matching Avançado)**

- **Código de documento da conciliação**: cada par (automático ou manual)
  recebe um código no formato `CONC-{ESCOPO}-{8 chars hex}`, gerado por hash
  determinístico dos IDs internos das duas linhas casadas — **estável**:
  rodar o matching de novo, em outro dia, para o mesmo par de lançamentos,
  sempre produz o mesmo código (decisão validada com a usuária). Ver
  `matching.gerar_codigo_conciliacao()`.
- **Match manual**: na aba, abaixo dos resultados, um expansor "🔗 Registrar
  match manual" permite escolher uma linha sem correspondência de cada lado
  (Adquirente + contraparte), ver a diferença de valor/data antes de
  confirmar (sem bloquear — é uma decisão humana), e registrar como match
  com 100% de confiança, campo de observação opcional e o mesmo código
  estável. A lista de matches manuais da sessão pode ser desfeita
  individualmente (as linhas voltam para "sem correspondência"). Uma nova
  execução do matching automático reinicia os matches manuais da sessão (o
  conjunto de "sem correspondência" é recalculado do zero).
- A tabela de pares e as exportações agora combinam automáticos + manuais,
  com o código de conciliação e a flag `manual` como primeiras colunas.

**Bug corrigido:**
- **Erro "cannot convert float NaN to integer" ao exportar para Excel.**
  Quando uma coluna fica 100% nula (ex.: `id_transacao_rec`, que só existe
  no escopo Adquirente×Recebimentos e fica vazia no escopo Pagamentos), o
  pandas mantém `NaN` mesmo depois de `.astype(str)` — isso quebrava o
  cálculo de largura da coluna. Corrigido em `to_excel()` com um valor
  padrão para esse caso.
- **Resultados do Matching Avançado somem ao mexer no slider de confiança
  ou ao exportar.** Toda a exibição estava dentro do `if st.button(...)`;
  qualquer interação com widgets internos (slider, botões de exportar)
  disparava um rerun em que o botão "Executar matching" voltava a `False`,
  escondendo a análise inteira. Corrigido guardando o resultado em
  `st.session_state` — o cálculo pesado só roda ao clicar em "Executar",
  mas a exibição e os filtros sobrevivem a reruns.

**Lembrete: o que já existia (waterfall multi-nível)**

Nova aba com motor de conciliação linha-a-linha entre Adquirente e
Pagamentos/Recebimentos, seguindo a especificação de negócio: cascata de
9 níveis de prioridade (1. campo único exato → 2. campo+valor → 3.
campo+parcela → 4. composto → 5. cruzamento entre identificadores → 6.
cruzamento com Fatura → 7. cruzamento com ID da Transação → 8. busca em
texto no memorando → 9. fallback aproximado: valor±R$0,01, valor+data,
TID/NSU sem zeros à esquerda ou por últimos dígitos, ARP parcial,
similaridade textual), cada match com um % de confiança.

- **Ver `matching.py`** para a especificação completa, o mapeamento de
  campos validado contra a base e as ~40 regras na ordem exata de prioridade.
- **Mapeamento de "ID da Transação"** (decisão a revisar com o time): ADQ×PAG
  usa `custcolcustcol_n_pagamento` (linha Adquirente) == `tranid` do CustPymt;
  ADQ×REC usa `custcolcustcoldata_idsaque` (nº do saque), que é **N:1** (várias
  vendas formam 1 depósito) — tratado corretamente pelo motor. Testamos
  também o campo "Transação NNNNNN" do memorando (possível ID do gateway
  Vindi), mas ele não aparece de forma consistente nos dois lados para a
  mesma venda, por isso não foi usado.
- **Confiança**: os pontos fixos da tabela de negócio (100/95/90/80/60%)
  foram replicados exatamente; combinações sem percentual explícito (a
  maioria dos Níveis 3, 5, 6 e 7) foram interpoladas — documentado regra a
  regra em `matching.py` e sinalizado na tela para revisão.
- **Performance**: os Níveis 1-7 são hash-joins vetorizados (rápidos mesmo
  com milhares de linhas). Os Níveis 8 (busca em texto) e 9 (fallback
  aproximado) são O(n×m) — por isso o período é **obrigatório** nesta aba
  (comece com poucos dias) e a etapa de similaridade textual tem um limite
  de pares próprio e configurável (a etapa mais lenta é pulada, com aviso na
  tela, se o volume remanescente for grande — as demais continuam normalmente).

**Bugs corrigidos:**
- **Erro "Invalid limit and offset values" em qualquer aba.** A paginação
  avançava o offset pela quantidade de linhas efetivamente retornada, em vez
  do `limit` solicitado. Em consultas com `GROUP BY`/`HAVING` (Pagamentos por
  DOC, Conciliação) o NetSuite pode devolver uma página "curta" ainda
  reportando `hasMore=true`, gerando um offset que não é múltiplo de 1000 e
  a API rejeita a próxima página. Corrigido em `netsuite_client.py`: o offset
  agora sempre avança pelo `limit` da página anterior, e a paginação para
  quando a página vem menor que o solicitado (testado com mock reproduzindo
  o cenário exato do erro).
- **Erro "Unknown identifier 'a.acctnumber'" no razão detalhado de
  Recebimentos.** Sobrou uma referência a um `JOIN` de conta que havia sido
  removido (substituído por `BUILTIN.DF`). Corrigido em `queries.py`
  (`q_recebimentos_detalhe`).

**Melhorias:**
- **Filtro de Subsidiárias enxuto**: a sidebar agora mostra só as 5
  subsidiárias com conta transitória mapeada (S3ENG, INEXT, MN, ALTO QI
  EDUCATION, QIHUB), em vez das 8 subsidiárias do NetSuite (Controladora,
  Eliminação, UNIGOV etc., que nunca batem com nenhuma conta do app).
- **Contas filtradas pela subsidiária selecionada**: ao marcar uma
  subsidiária, o multiselect de "Contas" em cada aba passa a mostrar só as
  contas daquela subsidiária + as contas "globais" (ex.: `ADQUIRENTE -
  CIELO/YAPAY/REDE/SAFRAPAY`, que não têm subsidiária específica e por isso
  valem para todas).
- **Filtro de data em todas as abas**, com toggle "Todo o período" +
  campos "Data início"/"Data fim" (padronizado — antes só existia em
  Recebimentos/Adquirente/Conciliação). Em Pagamentos, o filtro vem com um
  aviso: como o `CustPymt` e o `Journal` de baixa de um mesmo DOC podem cair
  em datas diferentes, filtrar por período pode fazer um DOC já zerado
  aparecer como divergente — "Todo o período" continua sendo o modo
  recomendado para a visão de exceções.
- **Nomes completos das contas selecionadas**: como o multiselect trunca
  nomes longos nas "pills", cada aba agora mostra um expansor "Nomes
  completos das contas selecionadas" logo abaixo do filtro.
- **Layout do filtro de data do Adquirente corrigido**: toggle e os dois
  campos de data agora ficam na mesma linha, com rótulos claros ("Data
  início" / "Data fim") — antes o layout ficava desalinhado e os rótulos de
  uma palavra só ("De"/"Até") eram mais suscetíveis a tradução automática
  estranha do navegador.

## Abas

| Aba | Conteúdo |
|---|---|
| 💳 Pagamentos | KPIs + consolidado por **DOC** (histórico completo) + drill-down do razão |
| 📥 Recebimentos | Consolidado **Conta > Data** com subtotais + razão detalhado |
| 🏦 Adquirente | Consolidado por conta/dia + razão com colunas calculadas do memorando |
| ⚖️ Conciliação | Cruzamento por **TID+NSU+ARP** com escopo configurável e aging |
| 🎯 Matching Avançado | Waterfall multi-nível linha-a-linha com classificação de confiança |

## Decisões de arquitetura (importante)

A base tem **~650 mil linhas GL** (Adquirente 388k · Pagamentos 256k ·
Recebimentos 8,7k). Por isso, **toda agregação roda dentro do NetSuite**
(SuiteQL) e o app baixa apenas:

- KPIs (1 linha);
- consolidados filtrados;
- **exceções** (DOCs não zerados, chaves divergentes);
- drill-downs sob demanda (1 DOC / 1 TID por vez).

### Regras de negócio validadas contra a base (jul/2026)

1. **Tipos nas transitórias de pagamento**: existem apenas `CustPymt`
   (Pagamento) e `Journal` (Lançamento). Não há terceiro tipo.
2. **Regra DOC**: o Journal de baixa referencia o pagamento na coluna de linha
   `custcolcustcol_n_pagamento` (ex.: `PYMT126476`). Portanto:
   `DOC = CASE WHEN type='CustPymt' THEN tranid ELSE custcolcustcol_n_pagamento END`.
   Com essa regra, apenas **25 de 255.541 DOCs** não zeram (−R$ 15.789,68).
3. **Parsing do memorando** (Adquirente/Recebimentos): padrão
   `RA: TID xxx | NSU yyy | ARP zzz | Parcela n`, com **caixa mista**
   ("PARCELA 1" / "Parcela 2"), prefixo "RA:" opcional, campos opcionais e
   memos não estruturados. Regex com flag `'i'`; campos ausentes ficam nulos.
4. **Campos customizados**:
   - Pagamentos (corpo): `custbody_nscs_tid`, `custbody_nscs_nsu`,
     `custbody_nscs_arp`, `custbodycustbody_nscs_idcobranca`.
   - Nomes/descrições (período, entidade, conta, subsidiária, status, criado
     por) resolvidos via `BUILTIN.DF()` — os joins em `accountingperiod`,
     `entity` e `employee` **não são acessíveis** pelo papel de integração.
   - Linha: `custcolcustcol_n_pagamento`, `custcolcustcoldata_idsaque`,
     `custcoldata_recebimento`, `custcolcustcol_id_fatura`,
     `custcolcustcol_id_cobranca`.
5. **Regras de divergência por escopo (dupla partida, validadas)**:
   - **Só Adquirente**: `(débitos − créditos)` da chave ≠ 0 — a entrada da
     venda vs a baixa do saque na mesma família de contas;
   - **Adquirente × Pagamentos**: `débitos ADQ ≠ débitos PAG` — as duas
     origens registram a mesma transação **a débito** (o CustPymt debita a
     transitória de pagamento e o Journal de venda debita a adquirente), então
     a checagem correta é de **valores**, não de soma zero. Teste real
     (1-5/jun/2026): a regra soma-zero marcava 2.570 chaves; a comparação de
     valores identificou os **115 divergentes reais**;
   - **Adquirente × Recebimentos**: `créditos ADQ ≠ débitos REC` — o saque
     credita a adquirente e debita a transitória de recebimento.

   Divergência ≠ erro: inclui parcelas legitimamente a receber (D+30).
   O app classifica com aging (0-30 / 31-60 / 61-90 / 90+):
   - 🟡 **EM ABERTO (a receber)** — diferença devedora dentro do prazo de
     liquidação (configurável, padrão 45 dias) → timing;
   - 🔴 **INVESTIGAR** — fora do prazo ou diferença credora.
6. **Mapeamento conta → subsidiária** (`queries.detectar_subsidiaria`): o
   código detecta a subsidiária pelo sufixo do nome da conta ("- S3ENG",
   "- MN", "(Educ)", "(QiHub)" etc.) usando a mesma função tanto para as
   contas quanto para os nomes de subsidiária vindos do NetSuite — garante
   que o filtro da sidebar e o filtro de contas usem sempre o mesmo
   critério. Contas sem sufixo de subsidiária (os 4 adquirentes genéricos)
   são tratadas como globais e aparecem independente do filtro.

## Setup

### 1. Criar a integração (uma vez, por um administrador NetSuite)

1. **Setup > Integration > Manage Integrations > New**
   - Token-Based Authentication: ✅ · Authorization Code Grant/OAuth 2.0: opcional
   - Guarde **Consumer Key/Secret** (só aparecem uma vez).
2. **Setup > Users/Roles > Access Tokens > New**
   - Application: a integração criada · User/Role: usuário de integração
   - Guarde **Token ID/Secret**.
3. **Permissões mínimas do papel (role)**:
   - `REST Web Services` (Full) e `Log in using Access Tokens`
   - `SuiteAnalytics Workbook` (View) — necessário para SuiteQL
   - Transações (View): Pagamento de cliente, Lançamento contábil
   - Listas (View): Contas, Subsidiárias, Períodos contábeis, Funcionários, Clientes
   - ⚠️ **Contas do tipo Bank**: se futuramente forem incluídas contas de banco,
     o papel precisa de `Bank Register` / Banking (View) — sem isso o SuiteQL
     retorna **zero linhas silenciosamente** (limitação já observada nesta conta).

### 2. Instalar e rodar

```bash
cd conciliacao-transitorias
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env    # e preencha as credenciais
streamlit run app.py
```

## Limitações conhecidas

- API REST SuiteQL: máx. **1.000 linhas/página** e **100.000 linhas/consulta**.
  O razão detalhado da Adquirente exige filtro de período por isso.
- SuiteQL não suporta CTEs (`WITH`) — as consultas usam subqueries inline.
- Datas são serializadas em ISO (`TO_CHAR ... 'YYYY-MM-DD'`) nas consultas para
  eliminar ambiguidade DD/MM vs MM/DD.
- A tela de divergências carrega até o limite escolhido (ordenado por |valor|);
  a exportação Excel/CSV respeita o mesmo limite — para a base completa,
  particione por subsidiária/período.

## Estrutura

```
conciliacao-transitorias/
├── app.py               # UI Streamlit (5 abas)
├── queries.py           # Consultas SuiteQL validadas + regras de negócio
├── matching.py          # Motor de waterfall matching (Matching Avançado)
├── netsuite_client.py   # Cliente REST (OAuth1 TBA, paginação, retry 429)
├── requirements.txt
├── .env.example
└── README.md
```

> ⚠️ Ferramenta de apoio à conciliação. Os resultados devem ser revisados pela
> contabilidade antes do fechamento e do sign-off.
