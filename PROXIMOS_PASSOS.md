# Próximos passos

Registro de onde este trabalho parou, pra continuar em outro dispositivo sem precisar reconstruir o contexto. Ver `ARQUITETURA.md` pra entender a estrutura; este arquivo é só a lista do que falta.

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

Isto é o trabalho combinado que ainda não começou. `pipeline.py` só adicionou travas genéricas (visibilidade, não correção) — a extração em si de cada distribuidora não foi revisada.

**Ponto de partida sugerido:** a aba Dashboards já mostra **217 faturas marcadas como `SUSPEITO`** (valores que não fecham: água+esgoto+taxas ≠ total, ou taxa negativa) — 109 SANEAGO, 61 SAAE_CORUMBA, 39 IPAMERI, 5 DEMAE, 3 SAE. Dá pra abrir a aba Dados, filtrar por essas distribuidoras e olhar as linhas com o badge "⚠ suspeita" como amostra guiada de onde os regex estão errando.

**Caso concreto já identificado** (documentado no commit da interface web): em `corrigir_distribuicao_financeira` (dentro de `enriquecer_com_contas_json`), quando `contas.json` marca `SMRSU=False` pra uma conta mas o extrator jogou um valor real de "taxa básica" (não SMRSU) em `VALOR_TAXAS_EXTRAS`, esse valor é zerado — descasando do total. Aconteceu com BURITI_ALEGRE no teste. Vale decidir: o campo `VALOR_TAXAS_EXTRAS` devia ter uma subdivisão (SMRSU vs. outras taxas), ou a flag do `contas.json` está incompleta pra essas contas?

**Por distribuidora, olhar:**
- Precisão de regex de data/valor em cada `extrair_*` (`extratores/*.py`).
- O corte "à tesoura" (`AUTENTICAÇÃO NO VERSO` / `Autenticação Mecânica`) que gera as linhas residuais — dá pra cortar de um jeito que não sobre resíduo, em vez de só filtrar depois.
- Otimizar o fallback OCR (`extratores/ocr_fallback.py`): DPI/PSM do Tesseract, tempo de processamento em lotes grandes, taxa de acerto em PDF-imagem.

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
