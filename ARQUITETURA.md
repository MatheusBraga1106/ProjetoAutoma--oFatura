# Arquitetura do projeto

Este documento explica como o repositório está organizado, o que cada parte faz e como elas se conectam. É o complemento técnico do `README.md` (que fica focado no "o quê" e no "como rodar"); aqui o foco é o "como está construído".

## Visão geral

O projeto tem três pontos de entrada para o mesmo pipeline de extração de faturas:

1. **CLI** (`Main.py`) — reextração/manutenção em lote: processa uma pasta (`--pasta`) ou um prefixo do storage (`--s3-prefixo`), reprocessa o que mudou (`--reprocessar-banco`), recalcula derivados (`--recalcular`) e exporta CSV (`--exportar-csv`).
2. **Web** (`api.py` + `templates/` + `static/`) — upload de arquivos/pastas pelo navegador, com progresso em tempo real e telas pra explorar o que já foi consolidado.
3. **Worker** (`worker.py`) — processa a fila de jobs criada pela web (ou pelo `Main.py --enfileirar`).

Os três **compartilham a mesma lógica** através de `pipeline.py` (roteamento, extratores, cruzamento, enriquecimento, suspeitas) e de `ingestao.py` (gravação no banco, deduplicação, versões, jobs). Isso é a decisão arquitetural mais importante do repositório: antes, `Main.py` e `api.py` tinham cada um sua própria cópia (levemente divergente) do roteamento, do cruzamento de dados e da gravação — o que já causou bugs reais. Qualquer mudança nessa lógica central vai em `pipeline.py`/`ingestao.py`, nunca duplicada nos pontos de entrada.

```
                ┌─────────────┐    ┌──────────────┐
                │ pipeline.py │ ←─ │  ingestao.py │ ──→ banco (Postgres/SQLite) + storage S3
                └─────────────┘    └──────┬───────┘
              ┌────────────────────┬──────┴─────────────┐
        ┌─────▼─────┐        ┌─────▼─────┐        ┌─────▼─────┐
        │  Main.py  │        │  api.py   │  jobs  │ worker.py │
        │   (CLI)   │        │   (web)   │ ─────→ │  (fila)   │
        └───────────┘        └───────────┘        └───────────┘
```

## Persistência (banco + storage)

- **Onde fica cada coisa**: PDFs e textos extraídos num storage S3 (em produção, o Silo no próprio Dokploy; em dev, sem `S3_BUCKET`, uma pasta local); faturas e jobs num banco (Postgres em produção; sem `DATABASE_URL`, SQLite em `dados_saida/faturas.db`, só pra dev/testes). CSV não é mais fonte de verdade — é exportação gerada do banco. Decisão e alternativas em `docs/decisao-armazenamento.md`; schema em `banco/modelos.py`.
- **Arquivo duplicado**: identidade pelo SHA-256 do conteúdo. O mesmo PDF nunca é guardado duas vezes, mas todos os caminhos pelos quais ele chegou ficam em `arquivo_origens` (o caminho/pasta é o que identifica a distribuidora).
- **Fatura duplicada**: chave natural normalizada `empresa|NUM_FATURA|MES_ANO_REF|CONTA_DV`. Chave incompleta nunca some nem se funde com outro arquivo — fica presa ao arquivo de origem e ganha `ALERTA_CHAVE`.
- **Reextração**: a versão de cada extrator é o hash do arquivo-fonte dele (`pipeline.versao_extrator`). Corrigiu um extrator → `python Main.py --reprocessar-banco --empresa X` reextrai só aquela distribuidora e guarda a versão anterior em `fatura_versoes`. Reenviar o mesmo arquivo sem mudança de código não gera trabalho nenhum.
- **Jobs**: tabela `jobs` + `job_arquivos` + `job_eventos` (o SSE da UI lê daqui). O worker reserva job com `FOR UPDATE SKIP LOCKED` + atualização condicional, mantém heartbeat, e job órfão (worker morto) é retomado de onde parou. Sobrevive a restart.
- **Limites** (upload web e OCR): 100 MB por arquivo (`UPLOAD_MAX_MB`; maior fatura real: 26 MB) e 40 Mpx por página rasterizada (`OCR_LIMITE_PIXELS_PAGINA`; acima disso a página é lida com DPI menor, em vez de recusada).

## Estrutura de pastas

```
.
├── Main.py                  # Ponto de entrada CLI — varre dados_entrada/
├── api.py                   # Ponto de entrada web — FastAPI, serve a UI e a API
├── pipeline.py               # Lógica compartilhada (roteamento, merges, suspeitas, versão do extrator)
├── ingestao.py               # Gravação no banco: dedup, versões, jobs, cruzamento entre lotes
├── worker.py                 # Processa a fila de jobs (python worker.py)
├── banco/                    # Modelos SQLAlchemy, sessão, storage S3/local, migração (python -m banco.migrar)
├── extratores/                # Um parser por distribuidora + fallbacks de texto
│   ├── saneago.py
│   ├── saneago_analitica.py
│   ├── sae.py
│   ├── codego.py
│   ├── aguas_ipameri.py
│   ├── buriti_alegre.py
│   ├── demae.py
│   ├── saae_abadiania.py
│   ├── saae_corumba.py
│   ├── saae_mineiros.py
│   ├── pdftotext_fallback.py  # 1ª tentativa: pdftotext (rápido)
│   └── ocr_fallback.py        # 2ª tentativa: OCR via Tesseract (PDF-imagem)
├── templates/
│   └── index.html            # Página única da UI (4 abas)
├── static/
│   ├── app.js                 # Upload, progresso (SSE), troca de abas
│   ├── dados.js                # Aba "Dados"
│   ├── dashboard.js            # Aba "Dashboards"
│   ├── erros.js                 # Aba "Erros"
│   ├── graficos.js             # Gráficos SVG reutilizáveis (barra/linha)
│   └── estilo.css              # Tema claro/escuro, tokens de gráfico
├── erros_reportados.py        # Banco SQLite da aba Erros (erros_reportados.db, NÃO versionado)
├── contas.json                # Cadastro de contas (dados reais — NÃO versionado)
├── contas.exemplo.json        # Mesma estrutura, com dados fictícios
├── dados_entrada/              # PDFs de entrada (NÃO versionado)
├── dados_saida/                 # CSVs consolidados, um por distribuidora (NÃO versionado)
├── conversor-pdfs.sh            # Script auxiliar (conversão em lote via shell)
├── Dockerfile
└── requirements.txt
```

`contas.json`, `dados_entrada/` e `dados_saida/` estão no `.gitignore` porque contêm dados reais (endereços, valores, nomes). É por isso que um clone novo do repositório roda, mas sem enriquecimento de dados até alguém colocar um `contas.json` de verdade na raiz.

## O pipeline, passo a passo

Tanto `Main.py` quanto `api.py` seguem a mesma sequência, só muda de onde vêm os arquivos (disco vs. upload):

1. **Identificação da distribuidora** — `pipeline.identificar_distribuidora(texto)` normaliza (minúsculas, sem acento) o nome do arquivo (+ pasta, no modo CLI) e casa contra a tabela `PADROES_DISTRIBUIDORA`. É o único lugar do repositório que sabe "qual palavra-chave identifica qual distribuidora" — adicionar uma nova distribuidora começa aqui.
2. **Texto do PDF** — primeiro tenta `pdftotext_fallback.converter_pdf_para_txt` (rápido, só funciona se o PDF tiver camada de texto real). Se o `.txt` resultante estiver vazio/curto/corrompido, cai pro `ocr_fallback.gerar_txt_via_ocr` (rasteriza cada página com PyMuPDF e roda Tesseract em cima — mais lento, mas funciona em fatura-imagem escaneada).
3. **Extração** — o `.txt` (nativo ou OCR) vai pro parser específico daquela distribuidora em `extratores/`, registrado em `pipeline.EXTRATORES_POR_EMPRESA`. Cada parser é independente: regex + heurísticas específicas do layout daquela fatura, devolvendo um `pandas.DataFrame`.
4. **Cruzamento SANEAGO + analítica** — `pipeline.mesclar_saneago_com_analitica`. A SANEAGO manda dois tipos de fatura (a normal e uma "analítica" só com dados de hidrômetro); esse passo casa as duas por `CONTA_DV` + `MES_ANO_REF`, com fallback pro hidrômetro mais recente daquela conta quando não há analítica exata do mês.
5. **Enriquecimento via `contas.json`** — `pipeline.enriquecer_com_contas_json`. Cruza pela conta (usando `normalizar_conta_dv`, que tira separador/zeros à esquerda pra não depender do extrator ter formatado a conta igual ao JSON) e traz `UNIDADE JUDICIÁRIA`, `ENDEREÇO`, `DISTRIBUIDORA` e as flags `AGUA`/`ESGOTO`/`SMRSU`. Essas flags decidem `corrigir_distribuicao_financeira`: zera um valor que caiu numa categoria que a conta não tem cadastrada (nunca desloca valor entre categorias — isso é trabalho do extrator, não do enriquecimento).
6. **Filtro de linhas vazias** — `pipeline.filtrar_linhas_vazias` descarta blocos residuais sem fatura/valor/consumo (efeito colateral do corte "à tesoura" de alguns extratores).
7. **Marcação de suspeitas** — `pipeline.marcar_suspeitas` sinaliza (`SUSPEITO=True`) quando `ÁGUA+ESGOTO+TAXAS` diverge do `TOTAL` em mais de 5 centavos, ou taxas saem negativas. Não corrige nada, só avisa — dá pra ver essas linhas na aba Dados/Dashboards.
8. **Gravação no banco** — `ingestao.py` grava cada arquivo numa transação só: faturas pela chave natural (dedup), versão nova com histórico quando o resultado mudou, e o texto extraído no storage (cache, pra não refazer OCR). Ver "Persistência" acima.

## `Main.py` (CLI)

Varre `dados_entrada/` recursivamente, roda os passos 1–8 pra cada `.txt` encontrado (gerando o `.txt` via passo 2 antes, em duas etapas prévias: todos os PDFs primeiro por `pdftotext`, depois só os que sobraram sem texto útil vão pro OCR — evita rodar OCR, que é lento, em PDF que já tem texto nativo). Uso:

```
python Main.py
```

## `api.py` (web)

Serve a UI (`GET /`, `templates/index.html` + `static/`) e expõe:

| Endpoint | O que faz |
|---|---|
| `GET /health`, `GET /manifest` | Healthcheck e metadados do serviço |
| `POST /extrair` | Extração avulsa, sem persistir — usado programaticamente |
| `POST /pipeline/jobs` | Sobe um lote de PDFs pro storage e cria um job na fila (o worker roda os passos 1–8 e grava no banco) |
| `GET /pipeline/jobs/{id}/eventos` | Progresso do job via **Server-Sent Events** (um evento por arquivo processado) |
| `GET /pipeline/jobs/{id}` | Snapshot do job (status + resultado agregado) |
| `GET /pipeline/jobs/{id}/csv/{empresa}` | Baixa o CSV de uma distribuidora após aquele job |
| `GET /pipeline/status-sistema` | `contas.json` presente? Tesseract/pdftotext resolvidos? Quais extratores existem? — alimenta o banner de aviso da UI |
| `GET /dados/empresas` | Lista distribuidoras com faturas no banco + contagem |
| `GET /dados/{empresa}` | Linhas paginadas (`pagina`, `tamanho_pagina`) + busca (`busca`) + `somente_suspeitas` de uma distribuidora |
| `GET /dados/{empresa}/csv` | Exporta em CSV as faturas daquela distribuidora (gerado do banco na hora) |
| `GET /dashboard/resumo` | Agregados pra aba Dashboards: KPIs, valor/suspeitas por empresa, série mensal, top 10 consumo/valor. `?empresa=X` filtra só a série mensal e os top 10 (KPIs e comparação por empresa continuam globais) |
| `GET /erros`, `POST /erros`, `PATCH /erros/{id}` | Fila de erros reportados pela aba Erros (listar/criar/marcar resolvido) — ver `erros_reportados.py` |

Jobs (`/pipeline/jobs/*`) ficam no banco e sobrevivem a restart. Em produção (Postgres) quem processa é o `worker.py`; com SQLite (dev) a própria API processa numa thread (`WORKER_EMBUTIDO`).

## A interface web (`templates/` + `static/`)

Uma página só (`index.html`), sem build step (JS vanilla direto, sem bundler/framework), com quatro abas trocadas via `static/app.js` (mostra/esconde `<div class="aba">`, sem recarregar a página):

- **Processar** — upload (arraste PDFs, ou selecione uma pasta inteira via `webkitdirectory`, replicando o walk de pastas do `Main.py`), progresso ao vivo consumindo o SSE de `/pipeline/jobs/{id}/eventos`, e o resumo do lote ao final.
- **Dados** (`static/dados.js`) — navega as faturas do banco, por distribuidora, com busca, filtro "Somente suspeitas" e paginação (server-side — a SANEAGO sozinha passa de 9 mil linhas). Cada linha tem um botão "Reportar erro" que troca pra aba Erros com a fatura já pré-preenchida.
- **Dashboards** (`static/dashboard.js` + `static/graficos.js`) — KPIs, comparação entre distribuidoras e evolução mensal. Os gráficos são SVG puro desenhado à mão (sem lib externa), seguindo as regras do skill de *dataviz* do projeto: hue sequencial único pra comparar magnitude (nunca uma cor por distribuidora), gráficos de linha de uma métrica só (nunca dois eixos Y), rótulo que só aparece quando cabe (senão vai pro tooltip).
- **Erros** (`static/erros.js`) — formulário pra reportar um problema numa fatura (distribuidora/fatura/conta/mês-ano + mensagem livre) e a lista dos já reportados, com filtro por status e botão pra marcar resolvido/reabrir. Persistido em `erros_reportados.py`, no mesmo banco das faturas (tabela `erros_reportados`, ligada à fatura quando a referência bate). Não altera nenhuma fatura; é só uma fila de revisão manual.

`static/estilo.css` define os tokens de cor (claro/escuro via `prefers-color-scheme`) usados tanto pela UI quanto pelos gráficos (`--serie-1` é o azul de referência do skill de dataviz, separado do `--cor-primaria` usado nos botões).

## `extratores/`

Arquitetura em plugin: cada distribuidora tem um `extrair_<nome>(caminho_txt, ...)` que recebe o caminho de um `.txt` e devolve um `DataFrame` (ou `None` se não reconheceu nada). Todos seguem o mesmo contrato de colunas de saída (`CONCESSIONARIA`, `NUM_FATURA`, `MES_ANO_REF`, `CONTA_DV`, `VALOR_AGUA`, `VALOR_ESGOTO`, `VALOR_TAXAS_EXTRAS`, `VALOR_TOTAL`, `CONSUMO_M3`, etc. — ver `MANIFEST["campos_saida"]` em `api.py`).

**Para adicionar uma nova distribuidora:**
1. Criar `extratores/nova_distribuidora.py` com `extrair_nova_distribuidora(caminho_txt)`.
2. Registrar em `pipeline.py`: uma entrada em `PADROES_DISTRIBUIDORA` (palavra-chave de identificação) e outra em `EXTRATORES_POR_EMPRESA` (a função). Isso já basta — `Main.py` e `api.py` pegam automaticamente, nenhum dos dois precisa mudar.

`pdftotext_fallback.py` e `ocr_fallback.py` não são extratores de dado — são as duas camadas de "conseguir texto a partir do PDF" que rodam *antes* de qualquer `extrair_*`.

## Dados versionados vs. não versionados

| Arquivo/pasta | Versionado? | Conteúdo |
|---|---|---|
| `contas.exemplo.json` | Sim | Estrutura do cadastro, com dados fictícios |
| `contas.json` | **Não** | Cadastro real (conta, unidade judiciária, endereço, flags água/esgoto/SMRSU) |
| `dados_entrada/` | **Não** | PDFs reais das faturas |
| `dados_saida/` | **Não** | Em dev: `faturas.db` (SQLite) e `armazenamento/` (storage local); CSVs antigos de antes do banco |

## Docker / deploy

Uma imagem só (`Dockerfile`: Python 3.12 em Debian trixie, `poppler-utils` + `tesseract-ocr` com português, usuário não-root) roda três papéis no `docker-compose.yml`: migração (`python -m banco.migrar`), app web (`uvicorn api:app`) e worker (`python worker.py`), junto com Postgres 17 e o storage S3 (Silo). Só a app é exposta, pelo domínio do Dokploy. Passo a passo, backups e reextração completa em `docs/deploy-dokploy.md`; decisão de armazenamento em `docs/decisao-armazenamento.md`.

## O que fica pra próxima fase

Ver `PROXIMOS_PASSOS.md`: primeiro deploy e reextração completa no servidor, os suspeitos restantes, e os extratores que ainda faltam (CHESP, SANESC, São Simão, Leopoldo de Bulhões) ou que não reconhecem nenhum dado (DEMAE Panamá).
