# Automação de Extração de Faturas

Ferramenta em Python para extrair, estruturar e consolidar dados de faturas de água/esgoto emitidas por **múltiplas distribuidoras**, a partir de faturas em PDF. Faz parte de uma pesquisa de eficientização de consumo de água em unidades prediais.

> ⚠️ Este repositório contém apenas o **código de extração**. Dados reais de contas, endereços e consumo não são versionados (ver `.gitignore`) — `contas.exemplo.json` mostra a estrutura esperada com dados fictícios.

## O que o projeto faz

1. **Conversão** — `conversor-pdfs.sh` converte as faturas de PDF para texto.
2. **Roteamento inteligente** — `Main.py` varre as pastas de entrada e identifica automaticamente qual distribuidora emitiu cada fatura (por nome de arquivo/pasta).
3. **Extração** — cada distribuidora tem um parser dedicado em `extratores/`, responsável por interpretar o layout específico daquela fatura e extrair os campos relevantes (consumo, valores, datas, hidrômetro etc).
4. **Consolidação** — as faturas extraídas vão para um banco de dados (Postgres em produção), com deduplicação por conteúdo do arquivo e histórico de versões quando um extrator é corrigido. CSV por distribuidora continua disponível como exportação.

## Distribuidoras suportadas

SANEAGO, SAE, CODEGO, Águas de Ipameri, Buriti Alegre Ambiental, DEMAE, SAAE Abadiânia, SAAE Corumbá, SAAE Mineiros.

Arquitetura modular: adicionar uma nova distribuidora é criar um novo parser em `extratores/` e registrar seu padrão de identificação e sua função em `pipeline.py`.

## Interface web

Upload de arquivos ou de uma pasta inteira, com progresso em tempo real, navegação das faturas (com filtro de suspeitas), dashboards e uma aba para reportar erros de extração. O pipeline completo (identificação → pdftotext → fallback OCR → extração → cruzamento SANEAGO/analítica → enriquecimento via `contas.json` → banco) é o mesmo do modo CLI.

Rodando localmente (sem Docker, com SQLite e storage em pasta dentro de `dados_saida/`):

```
pip install -r requirements.txt
uvicorn api:app --reload
```

Abra `http://localhost:8000/`. Reextração em lote de uma pasta: `python Main.py --pasta dados_entrada/` (`python Main.py --help` para as outras opções).

## Produção (Dokploy)

`docker-compose.yml` sobe Postgres, storage S3, a app e o worker de processamento. Passo a passo em [`docs/deploy-dokploy.md`](docs/deploy-dokploy.md); detalhes técnicos em [`ARQUITETURA.md`](ARQUITETURA.md).

## Stack

- Python (FastAPI, pandas, SQLAlchemy, regex)
- Postgres (dados tratados) + storage S3-compatível (PDFs)
- Tesseract (OCR) e poppler/pdftotext
- Jinja2 + JS vanilla (interface web, sem build step)
- Shell script para pré-processamento de PDFs

## Skills demonstradas

- Parsing de documentos semi-estruturados (PDF → texto → dados tabulares)
- Design modular orientado a plugins (um extrator por fonte de dado)
- Automação de pipeline de dados ponta a ponta
- Tratamento de encoding/normalização de texto (acentuação, variações de nome de arquivo)

## Status

Em uso ativo na pesquisa de eficientização de consumo de água.
