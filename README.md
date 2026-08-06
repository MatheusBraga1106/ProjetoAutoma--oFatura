# Automação de Extração de Faturas

Ferramenta em Python para extrair, estruturar e consolidar dados de faturas de água/esgoto emitidas por **múltiplas distribuidoras**, a partir de faturas em PDF. Faz parte de uma pesquisa de eficientização de consumo de água em unidades prediais.

> ⚠️ Este repositório contém apenas o **código de extração**. Dados reais de contas, endereços e consumo não são versionados (ver `.gitignore`) — `contas.exemplo.json` mostra a estrutura esperada com dados fictícios.

## O que o projeto faz

1. **Conversão** — `conversor-pdfs.sh` converte as faturas de PDF para texto.
2. **Roteamento inteligente** — `Main.py` varre as pastas de entrada e identifica automaticamente qual distribuidora emitiu cada fatura (por nome de arquivo/pasta).
3. **Extração** — cada distribuidora tem um parser dedicado em `extratores/`, responsável por interpretar o layout específico daquela fatura e extrair os campos relevantes (consumo, valores, datas, hidrômetro etc).
4. **Consolidação** — os dados extraídos de todas as faturas são organizados em DataFrames (pandas) por distribuidora e exportados para CSV, prontos para análise.

## Distribuidoras suportadas

SANEAGO, SAE, CODEGO, Águas de Ipameri, Buriti Alegre Ambiental, DEMAE, SAAE Abadiânia, SAAE Corumbá, SAAE Mineiros.

Arquitetura modular: adicionar uma nova distribuidora é criar um novo parser em `extratores/` e registrar seu padrão de identificação no roteador em `Main.py`.

## Stack

- Python (pandas, unicodedata, regex)
- Shell script para pré-processamento de PDFs

## Skills demonstradas

- Parsing de documentos semi-estruturados (PDF → texto → dados tabulares)
- Design modular orientado a plugins (um extrator por fonte de dado)
- Automação de pipeline de dados ponta a ponta
- Tratamento de encoding/normalização de texto (acentuação, variações de nome de arquivo)

## Status

Em uso ativo na pesquisa de eficientização de consumo de água.
