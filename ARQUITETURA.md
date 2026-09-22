# Arquitetura do projeto

Este documento explica como o repositório está organizado, o que cada parte faz e como elas se conectam. É o complemento técnico do `README.md` (que fica focado no "o quê" e no "como rodar"); aqui o foco é o "como está construído".

## Visão geral

O projeto tem dois jeitos de rodar o mesmo pipeline de extração de faturas:

1. **CLI** (`Main.py`) — varre uma pasta local (`dados_entrada/`) recursivamente e processa tudo que encontrar.
2. **Web** (`api.py` + `templates/` + `static/`) — upload de arquivos/pastas pelo navegador, com progresso em tempo real e telas pra explorar o que já foi consolidado.

Os dois **compartilham a mesma lógica de negócio** através de `pipeline.py`. Isso é a decisão arquitetural mais importante do repositório: antes da interface web existir, `Main.py` e `api.py` tinham cada um sua própria cópia (levemente divergente) do roteamento, do cruzamento de dados e da gravação em CSV — o que já causou bugs reais (ver seção "Bugs corrigidos" mais abaixo). Qualquer mudança nessa lógica central deve ser feita em `pipeline.py`, nunca duplicada de volta pros dois pontos de entrada.

```
                    ┌─────────────┐
                    │ pipeline.py │  ← lógica compartilhada
                    └──────┬──────┘
              ┌────────────┴────────────┐
        ┌─────▼─────┐              ┌────▼────┐
        │  Main.py  │              │ api.py  │
        │   (CLI)   │              │  (web)  │
        └───────────┘              └─────────┘
```

## Estrutura de pastas

```
.
├── Main.py                  # Ponto de entrada CLI — varre dados_entrada/
├── api.py                   # Ponto de entrada web — FastAPI, serve a UI e a API
├── pipeline.py               # Lógica compartilhada (roteamento, merges, CSV)
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
│   └── index.html            # Página única da UI (3 abas)
├── static/
│   ├── app.js                 # Upload, progresso (SSE), troca de abas
│   ├── dados.js                # Aba "Dados"
│   ├── dashboard.js            # Aba "Dashboards"
│   ├── graficos.js             # Gráficos SVG reutilizáveis (barra/linha)
│   └── estilo.css              # Tema claro/escuro, tokens de gráfico
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
8. **Gravação incremental** — `pipeline.salvar_incremental` grava em `dados_saida/banco_dados_<empresa>.csv`, ignorando faturas já salvas (chave `NUM_FATURA|MES_ANO_REF|CONTA_DV`) e realinhando as colunas do lote novo contra o cabeçalho já existente no CSV (trava contra desalinhamento se algum passo anterior mudar o conjunto de colunas produzidas — ex.: rodar sem `contas.json` presente).

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
| `POST /pipeline/jobs` | Sobe um lote de PDFs, roda o pipeline completo (passos 1–8) em background, grava em `dados_saida/` |
| `GET /pipeline/jobs/{id}/eventos` | Progresso do job via **Server-Sent Events** (um evento por arquivo processado) |
| `GET /pipeline/jobs/{id}` | Snapshot do job (status + resultado agregado) |
| `GET /pipeline/jobs/{id}/csv/{empresa}` | Baixa o CSV de uma distribuidora após aquele job |
| `GET /pipeline/status-sistema` | `contas.json` presente? Tesseract/pdftotext resolvidos? Quais extratores existem? — alimenta o banner de aviso da UI |
| `GET /dados/empresas` | Lista distribuidoras com CSV em `dados_saida/` + contagem de linhas |
| `GET /dados/{empresa}` | Linhas paginadas (`pagina`, `tamanho_pagina`) + busca (`busca`) de uma distribuidora |
| `GET /dados/{empresa}/csv` | Baixa o CSV consolidado daquela distribuidora |
| `GET /dashboard/resumo` | Agregados pra aba Dashboards: KPIs, valor/suspeitas por empresa, série mensal, top 10 consumo/valor |

Jobs (`/pipeline/jobs/*`) ficam em memória do processo (`dict` global `JOBS` em `api.py`) — não sobrevive a um restart do servidor nem escala pra múltiplas instâncias. Suficiente pro volume atual (uso único, poucas centenas de PDFs por lote).

A pasta de saída é configurável via env var `DADOS_SAIDA_DIR` (default: `dados_saida/` na raiz do projeto) — pensado pra permitir montar um volume persistente num deploy em nuvem sem mudar código.

## A interface web (`templates/` + `static/`)

Uma página só (`index.html`), sem build step (JS vanilla direto, sem bundler/framework), com três abas trocadas via `static/app.js` (mostra/esconde `<div class="aba">`, sem recarregar a página):

- **Processar** — upload (arraste PDFs, ou selecione uma pasta inteira via `webkitdirectory`, replicando o walk de pastas do `Main.py`), progresso ao vivo consumindo o SSE de `/pipeline/jobs/{id}/eventos`, e o resumo do lote ao final.
- **Dados** (`static/dados.js`) — navega os CSVs já consolidados, por distribuidora, com busca e paginação (server-side — a SANEAGO sozinha passa de 8 mil linhas).
- **Dashboards** (`static/dashboard.js` + `static/graficos.js`) — KPIs, comparação entre distribuidoras e evolução mensal. Os gráficos são SVG puro desenhado à mão (sem lib externa), seguindo as regras do skill de *dataviz* do projeto: hue sequencial único pra comparar magnitude (nunca uma cor por distribuidora), gráficos de linha de uma métrica só (nunca dois eixos Y), rótulo que só aparece quando cabe (senão vai pro tooltip).

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
| `dados_saida/` | **Não** | CSVs consolidados (dados reais extraídos) |

## Docker / deploy

`Dockerfile` empacota `api.py` (FastAPI) com `poppler-utils` (pdftotext) e `tesseract-ocr` (+ pacote de português) instalados via `apt`. Porta configurável via env `PORT` (default 8000). Não inclui `contas.json` nem `dados_entrada/` — isso é responsabilidade de quem faz o deploy (montar como volume, secret, etc.); sem `contas.json`, o pipeline roda igual mas sem enriquecimento (a UI avisa isso no banner de status).

## O que fica pra próxima fase

A revisão sistemática de cada `extrair_*` (precisão de regex, casos de borda de formato de data/valor, otimização do fallback OCR) é um trabalho separado, ainda não feito — o que existe hoje em `pipeline.py` são travas *genéricas* (não específicas de extrator) contra os efeitos mais graves dos bugs já encontrados: `normalizar_conta_dv` pro cruzamento com `contas.json`, `filtrar_linhas_vazias` pras linhas residuais, `marcar_suspeitas` pra sinalizar valores que não fecham, e o realinhamento de colunas em `salvar_incremental`. Nenhuma dessas travas corrige a extração em si — elas dão visibilidade e evitam que dado ruim se espalhe silenciosamente, até a extração de cada distribuidora ser revisada a fundo.
