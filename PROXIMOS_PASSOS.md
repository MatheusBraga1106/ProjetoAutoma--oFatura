# Próximos passos

Registro de onde este trabalho parou, pra continuar em outro dispositivo sem precisar reconstruir o contexto. Ver `ARQUITETURA.md` pra entender a estrutura; este arquivo é só a lista do que falta.

## Sessão de 2026-09-22 — revisão da Fase 2 (regex por distribuidora)

Com um `contas.json` real em mãos, entrei na "Fase 2" pendente abaixo (revisão sistêmica dos extratores). Commits `479a204`, `f7521b2`, `3446189`, `1188d90` (todos já no GitHub):

- **SANEAGO**: faturas via OCR sem ESGOTO cadastrado perdiam o valor de SMRSU (caía na categoria errada e era zerado). Corrigido usando as flags do `contas.json` pra desambiguar.
- **SAAE_ABADIANIA**: água/esgoto eram buscados só dentro do bloco errado do corte por "AUTENTICAÇÃO NO VERSO" (a 2ª via/canhoto, sem a tabela de valores) — saíam 0,00 na maioria das faturas. Corrigido buscando no texto inteiro do arquivo.
- **SAAE_MINEIROS**: OCR às vezes lê "(-)" como "(=)" no total, que saía zerado. Corrigido aceitando os dois.
- **Separação SMRSU vs. taxa genérica**: `VALOR_TAXAS_EXTRAS` era zerado quando `contas.json` marcava `SMRSU=False`, mas em 7 das 8 distribuidoras esse campo nunca foi SMRSU de verdade — é um resíduo genérico (`total - água - esgoto`) que pode ser qualquer taxa fixa real (`TARIFA BASICO OPERACIONAL`, `TARIFA BÁSICA`, `SERVIÇO BÁSICO ÁGUA`...). Criado `VALOR_OUTRAS_TAXAS`, nunca zerado por flag — `VALOR_TAXAS_EXTRAS` agora é exclusivo da SANEAGO.

Resultado nos dados reais (1.119 faturas): suspeitos caíram de 217 pra 63. Restam: 40 na IPAMERI (residual sempre negativo nessa conta — IRPJ maior que a tarifa básica, dinheiro certo, só fica sinalizado por ser negativo, não é bug), 21 na SANEAGO (contas.json com flag possivelmente invertida numa conta + 1 ruído de OCR), 1 na SAAE_CORUMBA (total ilegível por OCR numa fatura só) e 1 na SAE (residual negativo real, mesma família da IPAMERI).

Também adicionada a aba **Erros** (`static/erros.js` + `erros_reportados.py`, SQLite local) — formulário pra reportar problema numa fatura (com botão "Reportar erro" em cada linha da aba Dados, já pré-preenchido) e lista com status aberto/resolvido.

## Estado atual (o que já foi feito)

- Análise completa do repositório, com 3 bugs sistêmicos identificados e corrigidos de forma genérica em `pipeline.py` (não tocaram a lógica interna de nenhum extrator):
  1. `CONTA_DV` em formatos diferentes entre extratores e `contas.json` quebrava o enriquecimento pra ~5 de 9 distribuidoras → `normalizar_conta_dv`.
  2. Linhas fantasma (quase vazias) do corte "à tesoura" de alguns extratores → `filtrar_linhas_vazias`.
  3. CSV podia desalinhar coluna se `contas.json` estivesse ausente → `enriquecer_com_contas_json` sempre devolve o conjunto completo de colunas + `salvar_incremental` realinha contra o cabeçalho já gravado.
- `pipeline.py` criado como lógica compartilhada entre `Main.py` (CLI) e `api.py` (web) — antes duplicada e divergente.
- Interface web com 3 abas: **Processar** (upload + progresso via SSE), **Dados** (navegação paginada dos CSVs consolidados, com busca), **Dashboards** (KPIs, gráficos SVG por distribuidora e por mês, maiores consumos/valores).
- Tudo testado ponta a ponta com dados reais (Playwright + faturas de `dados_entrada/`), incluindo modo escuro.
- Repositório atualizado: commits `a1fbc76` (interface web) e `a85408e` (`ARQUITETURA.md`).

## Pendente — Fase 2: revisão sistêmica dos extratores

Grande parte já feita na sessão de 2026-09-22 (ver acima) — sobram 63 suspeitos (era 217), a maioria já entendida e não são bugs de regex. O que ainda falta:

- **SANEAGO, 21 restantes**: pelo menos 1 caso (conta `75696 2`, "Palácio da Justiça/Área Verde") em que a fatura só cobra ÁGUA mas o `contas.json` marca `AGUA=False, ESGOTO=True` — parece flag invertida no cadastro, não bug de código. Vale conferir com quem mantém o `contas.json`. Tem também 1 ruído de OCR isolado (dígito espúrio ",11" lido como valor de esgoto).
- **SAAE_CORUMBA, 1 restante** (fatura 637542): OCR devolveu "Declaração: as 47385" no lugar do valor total — texto corrompido demais pra recuperar por regex. Só resolve com OCR melhor (DPI/PSM) ou revisão manual do PDF original.
- **IPAMERI (40) e SAE (1)**: não são bugs — residual negativo real (retenção de IRPJ maior que a tarifa básica). Ficam sinalizados de propósito pra revisão humana, não pra "corrigir".
- **Fallback OCR em geral** (`extratores/ocr_fallback.py`): DPI/PSM do Tesseract, tempo de processamento em lotes grandes — os casos acima que travam em "OCR ilegível" só melhoram por aqui.
- Precisão de regex de data/valor nos `extrair_*` que ainda não foram auditados a fundo (BURITI_ALEGRE, CODEGO — este último sem dados reais testados ainda).

## Outras pendências menores (não bloqueantes)

- **Jobs em memória**: `JOBS` em `api.py` é um `dict` do processo — não sobrevive a restart do servidor, não escala pra múltiplas instâncias. Ok pro volume atual; revisitar se o uso crescer.
- **Identificação por nome de arquivo avulso**: no upload por arquivo único (não por pasta), a identificação da distribuidora (`identificar_distribuidora`) só tem o nome do arquivo — se o nome não contiver a palavra-chave (ex.: `215449 (1).pdf` da DEMAE, que só tem "demae" no nome da *pasta*), o upload falha com "não foi possível identificar". Upload por pasta (`webkitdirectory`) resolve isso porque manda o caminho relativo. Vale considerar um fallback manual ("selecionar distribuidora") na UI pra esses casos.
- **`/dashboard/resumo` sem cache**: recalcula tudo a cada request lendo os CSVs do zero. Rápido o suficiente hoje (~8,7 mil linhas), revisitar se `dados_saida/` crescer muito.
- **Gráfico de linha mensal é agregado, não por distribuidora**: decisão deliberada (evitar 9 séries categóricas num gráfico só, ver `ARQUITETURA.md`/skill de dataviz). Se fizer falta granularidade por distribuidora, a solução correta é small multiples (um gráfico pequeno por empresa), não cores diferentes na mesma linha.
- **Sem filtro de período na aba Dashboards** (mostra o dataset inteiro). Puro corte de escopo pela baixa urgência — adicionar depois se fizer falta.

## Para continuar em outro dispositivo

```
git clone https://github.com/MatheusBraga1106/ProjetoAutoma--oFatura.git
cd ProjetoAutomaçãoFatura
pip install -r requirements.txt
```

Depois:
1. Colocar um `contas.json` real na raiz (é gitignored — copiar de onde estava guardado, ou usar `contas.exemplo.json` como base só pra testar a estrutura).
2. Colocar os PDFs em `dados_entrada/` (também gitignored) se for rodar localmente.
3. Rodar `uvicorn api:app --reload` e abrir `http://localhost:8000/` — a aba Processar já avisa no banner se `contas.json`/Tesseract/pdftotext não forem encontrados.
4. Fora do Docker, `extratores/ocr_fallback.py` e `extratores/pdftotext_fallback.py` esperam `tesseract`/`pdftotext` no PATH (com fallback pra caminhos fixos de instalação do Windows deste dispositivo — ajustar/instalar via PATH no dispositivo novo em vez de depender desses caminhos fixos).
