import json
import os
import shutil
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

import pandas as pd
import pytesseract
from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# Os extratores originais imprimem emojis de debug (✅❌🔎...) via print().
# Num console/ambiente cujo stdout não seja UTF-8 (comum no Windows, e
# depende do locale da imagem base no Docker), isso derruba a extração
# inteira com UnicodeEncodeError antes mesmo de chegar no try/except do
# endpoint. Forçar UTF-8 aqui evita mexer nos prints de cada extrator.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from pipeline import (
    EMPRESAS_CONHECIDAS,
    EXTRATORES_NAO_IMPLEMENTADOS,
    EXTRATORES_POR_EMPRESA,
    PASTA_BASE,
    carregar_dataframe_empresa,
    enriquecer_com_contas_json,
    filtrar_linhas_vazias,
    identificar_distribuidora,
    linha_para_dados,
    listar_empresas_com_dados,
    marcar_suspeitas,
    mesclar_saneago_com_analitica,
    salvar_incremental,
)
from extratores.ocr_fallback import eh_texto_util, gerar_txt_via_ocr
from extratores.pdftotext_fallback import PDFTOTEXT_CMD, converter_pdf_para_txt

app = FastAPI(title="Extrator de Faturas de Água")

DIRETORIO_APP = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(DIRETORIO_APP, "templates"))
app.mount("/static", StaticFiles(directory=os.path.join(DIRETORIO_APP, "static")), name="static")

MANIFEST = {
    "id": "faturas-agua",
    "nome": "Extrator de Faturas de Água",
    "dominio": "agua",
    "versao": "1.0.0",
    "formatos_aceitos": ["pdf"],
    "campos_saida": [
        "CONCESSIONARIA", "NUM_FATURA", "MES_ANO_REF", "VENCIMENTO", "CONTA_DV",
        "NUM_HIDROMETRO", "NOME_CLIENTE", "LOGRADOURO", "CONSUMO_M3",
        "VALOR_AGUA", "VALOR_ESGOTO", "VALOR_TAXAS_EXTRAS", "VALOR_TOTAL",
    ],
}

PASTA_SAIDA_PADRAO = os.path.join(PASTA_BASE, "dados_saida")


def pasta_saida() -> str:
    """Diretório onde os CSVs consolidados são gravados. Configurável via
    DADOS_SAIDA_DIR pra permitir montar um volume persistente em deploys
    de nuvem sem mudar código."""
    return os.environ.get("DADOS_SAIDA_DIR", PASTA_SAIDA_PADRAO)


def texto_de(caminho_pdf: str) -> tuple[str, bool]:
    """Garante um .txt pareado, tentando pdftotext e caindo pro OCR se precisar.
    Devolve (caminho_txt, veio_de_ocr)."""
    caminho_txt = os.path.splitext(caminho_pdf)[0] + ".txt"
    converter_pdf_para_txt(caminho_pdf, caminho_txt)
    if eh_texto_util(caminho_txt):
        return caminho_txt, False
    gerar_txt_via_ocr(caminho_pdf, caminho_txt)
    return caminho_txt, True


def _validar_empresa(empresa: str) -> str:
    """Whitelist contra path traversal — "empresa" costuma vir direto da URL."""
    empresa_upper = empresa.upper()
    if empresa_upper not in EMPRESAS_CONHECIDAS:
        raise HTTPException(status_code=404, detail="Empresa desconhecida.")
    return empresa_upper


def _caminho_csv_empresa(empresa: str) -> str:
    empresa = _validar_empresa(empresa)
    return os.path.join(pasta_saida(), f"banco_dados_{empresa.lower()}.csv")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/manifest")
def manifest():
    return MANIFEST


@app.get("/")
def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


# =========================================================
# EXTRAÇÃO AVULSA (sem persistência) — endpoint original
# =========================================================

@app.post("/extrair")
def extrair(arquivos: List[UploadFile] = File(...)):
    resultados = []
    lotes_saneago = []  # [(nome_arquivo, dataframe)]
    lotes_analitica = []  # [(nome_arquivo, dataframe)]

    with tempfile.TemporaryDirectory() as pasta_tmp:
        for arquivo in arquivos:
            empresa = identificar_distribuidora(arquivo.filename or "")

            if empresa is None:
                resultados.append({
                    "arquivo_origem": arquivo.filename,
                    "status": "erro",
                    "erro": "Não foi possível identificar a distribuidora deste arquivo.",
                })
                continue

            if empresa in EXTRATORES_NAO_IMPLEMENTADOS:
                resultados.append({
                    "arquivo_origem": arquivo.filename,
                    "status": "erro",
                    "erro": f"Extrator para {empresa} ainda não foi implementado.",
                })
                continue

            # Nunca usar o nome recebido do cliente pra montar caminho em disco: pode
            # conter "/" (o chamador tem liberdade de incluir contexto de pasta no nome,
            # útil pra identificação — ver identificar_distribuidora) ou até ".." — um
            # nome de arquivo de cliente nunca é confiável como caminho de sistema.
            extensao = os.path.splitext(arquivo.filename)[1] or ".pdf"
            caminho_pdf = os.path.join(pasta_tmp, f"{uuid.uuid4().hex}{extensao}")
            with open(caminho_pdf, "wb") as f:
                f.write(arquivo.file.read())

            try:
                caminho_txt, veio_de_ocr = texto_de(caminho_pdf)
                df = EXTRATORES_POR_EMPRESA[empresa](caminho_txt, veio_de_ocr)
            except Exception as e:
                resultados.append({
                    "arquivo_origem": arquivo.filename,
                    "status": "erro",
                    "erro": str(e),
                })
                continue

            if df is None or df.empty:
                resultados.append({
                    "arquivo_origem": arquivo.filename,
                    "status": "erro",
                    "erro": "Nenhum dado reconhecido neste arquivo.",
                })
                continue

            if empresa == "SANEAGO_ANALITICA":
                lotes_analitica.append((arquivo.filename, df))
            elif empresa == "SANEAGO":
                lotes_saneago.append((arquivo.filename, df))
            else:
                for _, linha in df.iterrows():
                    resultados.append({
                        "arquivo_origem": arquivo.filename,
                        "status": "ok",
                        "dados": linha_para_dados(linha),
                    })

        if lotes_saneago:
            df_saneago = pd.concat([df for _, df in lotes_saneago], ignore_index=True)
            nomes_saneago = [nome for nome, df in lotes_saneago for _ in range(len(df))]

            if lotes_analitica:
                df_analitica = pd.concat([df for _, df in lotes_analitica], ignore_index=True)
                df_saneago = mesclar_saneago_com_analitica(df_saneago, df_analitica)

            for posicao, (_, linha) in enumerate(df_saneago.iterrows()):
                resultados.append({
                    "arquivo_origem": nomes_saneago[posicao],
                    "status": "ok",
                    "dados": linha_para_dados(linha),
                })
        elif lotes_analitica:
            # Sem fatura SANEAGO no mesmo lote pra cruzar — devolve os dados
            # do hidrômetro isolados mesmo assim (não descarta o arquivo).
            for nome, df in lotes_analitica:
                for _, linha in df.iterrows():
                    resultados.append({
                        "arquivo_origem": nome,
                        "status": "ok",
                        "dados": linha_para_dados(linha),
                    })

    return {"resultados": resultados}


# =========================================================
# PIPELINE COMPLETO (identificação + OCR + extração + cruzamento
# SANEAGO/analítica + enriquecimento contas.json + persistência
# incremental) — o mesmo que o Main.py faz varrendo pastas, aqui
# disparado por upload e acompanhável em tempo real pela UI.
# =========================================================

class StatusJob(str, Enum):
    PENDENTE = "pendente"
    PROCESSANDO = "processando"
    CONCLUIDO = "concluido"
    ERRO = "erro"


@dataclass
class Job:
    id: str
    total_arquivos: int
    status: StatusJob = StatusJob.PENDENTE
    eventos: list = field(default_factory=list)
    resultado: Optional[dict] = None


JOBS: dict[str, Job] = {}


def _comando_resolvido(cmd: str) -> bool:
    if os.path.isabs(cmd):
        return os.path.exists(cmd)
    return shutil.which(cmd) is not None


def _registrar_evento(job: Job, tipo: str, dados: dict):
    job.eventos.append({"tipo": tipo, "dados": dados})


def processar_job(job_id: str, pasta_tmp: str, arquivos_info: List[tuple]):
    job = JOBS[job_id]
    job.status = StatusJob.PROCESSANDO
    dados_por_empresa = {empresa: [] for empresa in EMPRESAS_CONHECIDAS}
    arquivos_resultado = []

    try:
        for indice, (nome_relativo, caminho_pdf) in enumerate(arquivos_info, start=1):
            base_evento = {"arquivo": nome_relativo, "indice": indice, "total": job.total_arquivos}

            empresa = identificar_distribuidora(nome_relativo)

            if empresa is None:
                arquivos_resultado.append({
                    "arquivo_origem": nome_relativo, "status": "erro",
                    "erro": "Não foi possível identificar a distribuidora deste arquivo.",
                })
                _registrar_evento(job, "progresso", {**base_evento, "status": "erro",
                                                       "detalhe": "distribuidora não identificada"})
                continue

            if empresa in EXTRATORES_NAO_IMPLEMENTADOS:
                arquivos_resultado.append({
                    "arquivo_origem": nome_relativo, "status": "erro",
                    "erro": f"Extrator para {empresa} ainda não foi implementado.",
                })
                _registrar_evento(job, "progresso", {**base_evento, "status": "erro",
                                                       "detalhe": f"extrator {empresa} não implementado"})
                continue

            try:
                caminho_txt, veio_de_ocr = texto_de(caminho_pdf)
                df = EXTRATORES_POR_EMPRESA[empresa](caminho_txt, veio_de_ocr)
            except Exception as e:
                arquivos_resultado.append({"arquivo_origem": nome_relativo, "status": "erro", "erro": str(e)})
                _registrar_evento(job, "progresso", {**base_evento, "status": "erro", "detalhe": str(e)})
                continue

            if df is None or df.empty:
                arquivos_resultado.append({
                    "arquivo_origem": nome_relativo, "status": "erro",
                    "erro": "Nenhum dado reconhecido neste arquivo.",
                })
                _registrar_evento(job, "progresso", {**base_evento, "status": "erro",
                                                       "detalhe": "nenhum dado reconhecido"})
                continue

            df["ARQUIVO_ORIGEM"] = os.path.basename(nome_relativo)
            df["PASTA_ORIGEM"] = os.path.dirname(nome_relativo)
            dados_por_empresa[empresa].append(df)
            arquivos_resultado.append({
                "arquivo_origem": nome_relativo, "status": "ok", "empresa": empresa, "linhas": len(df),
            })
            _registrar_evento(job, "progresso", {**base_evento, "status": "ok", "empresa": empresa})

        # Cruzamento SANEAGO + analítica (hidrômetros), mesma lógica do Main.py
        if dados_por_empresa["SANEAGO"] and dados_por_empresa["SANEAGO_ANALITICA"]:
            df_saneago = pd.concat(dados_por_empresa["SANEAGO"], ignore_index=True)
            df_analitica = pd.concat(dados_por_empresa["SANEAGO_ANALITICA"], ignore_index=True)
            dados_por_empresa["SANEAGO"] = [mesclar_saneago_com_analitica(df_saneago, df_analitica)]
        dados_por_empresa["SANEAGO_ANALITICA"] = []

        caminho_json = os.path.join(PASTA_BASE, "contas.json")
        diretorio_saida = pasta_saida()
        os.makedirs(diretorio_saida, exist_ok=True)

        resumo_empresas = {}
        for empresa, lista_de_dfs in dados_por_empresa.items():
            if not lista_de_dfs:
                continue

            df_novo_lote = pd.concat(lista_de_dfs, ignore_index=True)
            df_novo_lote, info_enriquecimento = enriquecer_com_contas_json(df_novo_lote, caminho_json)
            df_novo_lote, descartadas = filtrar_linhas_vazias(df_novo_lote)

            if df_novo_lote.empty:
                resumo_empresas[empresa] = {
                    "adicionadas": 0, "duplicadas": 0, "suspeitas": 0,
                    "descartadas": descartadas, **info_enriquecimento,
                }
                continue

            df_novo_lote = marcar_suspeitas(df_novo_lote)
            caminho_csv = os.path.join(diretorio_saida, f"banco_dados_{empresa.lower()}.csv")
            resultado = salvar_incremental(df_novo_lote, caminho_csv)
            resumo_empresas[empresa] = {
                **resultado,
                "suspeitas": int(df_novo_lote["SUSPEITO"].sum()),
                "descartadas": descartadas,
                **info_enriquecimento,
            }

        job.resultado = {"arquivos": arquivos_resultado, "empresas": resumo_empresas}
        job.status = StatusJob.CONCLUIDO
        _registrar_evento(job, "concluido", job.resultado)
    except Exception as e:
        job.status = StatusJob.ERRO
        job.resultado = {"erro": str(e)}
        _registrar_evento(job, "erro", {"erro": str(e)})
    finally:
        shutil.rmtree(pasta_tmp, ignore_errors=True)


@app.get("/pipeline/status-sistema")
def status_sistema():
    caminho_json = os.path.join(PASTA_BASE, "contas.json")
    return {
        "contas_json_encontrado": os.path.exists(caminho_json),
        "tesseract_resolvido": _comando_resolvido(pytesseract.pytesseract.tesseract_cmd),
        "pdftotext_resolvido": _comando_resolvido(PDFTOTEXT_CMD),
        "distribuidoras_implementadas": sorted(EXTRATORES_POR_EMPRESA),
        "distribuidoras_nao_implementadas": sorted(EXTRATORES_NAO_IMPLEMENTADOS),
        "pasta_saida": pasta_saida(),
    }


@app.post("/pipeline/jobs")
async def criar_job(background_tasks: BackgroundTasks, arquivos: List[UploadFile] = File(...)):
    if not arquivos:
        raise HTTPException(status_code=400, detail="Nenhum arquivo enviado.")

    arquivos_pdf = [a for a in arquivos if (a.filename or "").lower().endswith(".pdf")]
    if not arquivos_pdf:
        raise HTTPException(status_code=400, detail="Nenhum PDF encontrado no envio.")

    job_id = uuid.uuid4().hex
    pasta_tmp = tempfile.mkdtemp(prefix=f"pipeline_{job_id}_")
    arquivos_info = []

    for arquivo in arquivos_pdf:
        # nome_relativo é só pra identificação/rótulo — o arquivo em disco usa
        # nome opaco (uuid), nunca o nome vindo do cliente (ver comentário em /extrair).
        nome_relativo = arquivo.filename or "arquivo.pdf"
        caminho_pdf = os.path.join(pasta_tmp, f"{uuid.uuid4().hex}.pdf")
        conteudo = await arquivo.read()
        with open(caminho_pdf, "wb") as f:
            f.write(conteudo)
        arquivos_info.append((nome_relativo, caminho_pdf))

    job = Job(id=job_id, total_arquivos=len(arquivos_info))
    JOBS[job_id] = job
    background_tasks.add_task(processar_job, job_id, pasta_tmp, arquivos_info)

    return {"job_id": job_id, "total_arquivos": job.total_arquivos}


@app.get("/pipeline/jobs/{job_id}")
def status_job(job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job não encontrado.")
    return {
        "job_id": job.id,
        "status": job.status,
        "total_arquivos": job.total_arquivos,
        "resultado": job.resultado,
    }


@app.get("/pipeline/jobs/{job_id}/eventos")
async def eventos_job(job_id: str):
    import asyncio

    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job não encontrado.")

    async def streamer():
        indice = 0
        while True:
            while indice >= len(job.eventos) and job.status not in (StatusJob.CONCLUIDO, StatusJob.ERRO):
                await asyncio.sleep(0.4)
            while indice < len(job.eventos):
                evento = job.eventos[indice]
                indice += 1
                yield f"event: {evento['tipo']}\ndata: {json.dumps(evento['dados'], ensure_ascii=False)}\n\n"
            if job.status in (StatusJob.CONCLUIDO, StatusJob.ERRO):
                break

    return StreamingResponse(streamer(), media_type="text/event-stream")


@app.get("/pipeline/jobs/{job_id}/csv/{empresa}")
def baixar_csv(job_id: str, empresa: str):
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job não encontrado.")

    caminho_csv = _caminho_csv_empresa(empresa)
    if not os.path.exists(caminho_csv):
        raise HTTPException(status_code=404, detail="CSV não encontrado para essa empresa.")

    return FileResponse(caminho_csv, filename=os.path.basename(caminho_csv), media_type="text/csv")


# =========================================================
# DADOS CONSOLIDADOS (aba "Dados" da UI) — navegar os CSVs de
# dados_saida/ já gravados, por distribuidora, com busca e paginação.
# =========================================================

@app.get("/dados/empresas")
def dados_empresas():
    return {"empresas": listar_empresas_com_dados(pasta_saida())}


@app.get("/dados/{empresa}")
def dados_empresa(empresa: str, pagina: int = 1, tamanho_pagina: int = 50, busca: str = ""):
    empresa = _validar_empresa(empresa)
    df = carregar_dataframe_empresa(pasta_saida(), empresa)
    if df is None:
        raise HTTPException(status_code=404, detail="Nenhum dado encontrado para essa empresa.")

    pagina = max(pagina, 1)
    tamanho_pagina = min(max(tamanho_pagina, 1), 500)

    if busca.strip():
        alvo = busca.strip().lower()
        colunas_busca = [c for c in ["NOME_CLIENTE", "CONTA_DV", "NUM_FATURA", "ARQUIVO_ORIGEM"] if c in df.columns]
        mascara = pd.Series(False, index=df.index)
        for coluna in colunas_busca:
            mascara = mascara | df[coluna].astype(str).str.lower().str.contains(alvo, na=False, regex=False)
        df = df[mascara]

    df = df.copy()
    df["_DATA_ORD"] = pd.to_datetime(df.get("MES_ANO_REF", ""), format="%m/%Y", errors="coerce")
    df = df.sort_values("_DATA_ORD", ascending=False, na_position="last").drop(columns=["_DATA_ORD"])

    total = len(df)
    inicio = (pagina - 1) * tamanho_pagina
    pagina_df = df.iloc[inicio:inicio + tamanho_pagina]

    return {
        "empresa": empresa,
        "total": total,
        "pagina": pagina,
        "tamanho_pagina": tamanho_pagina,
        "linhas": [linha_para_dados(linha) for _, linha in pagina_df.iterrows()],
    }


@app.get("/dados/{empresa}/csv")
def dados_empresa_csv(empresa: str):
    caminho_csv = _caminho_csv_empresa(empresa)
    if not os.path.exists(caminho_csv):
        raise HTTPException(status_code=404, detail="CSV não encontrado para essa empresa.")
    return FileResponse(caminho_csv, filename=os.path.basename(caminho_csv), media_type="text/csv")


# =========================================================
# DASHBOARD (aba "Dashboards" da UI) — visão agregada de todas as
# distribuidoras: KPIs, comparação por empresa, série mensal e maiores
# consumos/valores. Sem cache — o volume total é pequeno pra pandas.
# =========================================================

@app.get("/dashboard/resumo")
def dashboard_resumo():
    saida = pasta_saida()
    empresas_info = listar_empresas_com_dados(saida)

    kpis = {"total_faturas": 0, "valor_total": 0.0, "consumo_total": 0.0, "total_suspeitas": 0}
    por_empresa = []
    serie_mensal_acumulador: dict = {}
    linhas_top_consumo = []
    linhas_top_valor = []

    for info in empresas_info:
        empresa = info["empresa"]
        df = carregar_dataframe_empresa(saida, empresa)
        if df is None or df.empty:
            continue

        valor_total_empresa = float(df["VALOR_TOTAL"].fillna(0).sum())
        consumo_total_empresa = float(df["CONSUMO_M3"].fillna(0).sum())
        suspeitas_empresa = int(df["SUSPEITO"].sum())

        kpis["total_faturas"] += len(df)
        kpis["valor_total"] += valor_total_empresa
        kpis["consumo_total"] += consumo_total_empresa
        kpis["total_suspeitas"] += suspeitas_empresa

        por_empresa.append({
            "empresa": empresa,
            "valor_total": round(valor_total_empresa, 2),
            "faturas": len(df),
            "suspeitas": suspeitas_empresa,
        })

        # Série mensal: soma por mês, acumulando entre todas as distribuidoras
        # (uma série só no gráfico final — ver Contexto do plano sobre o teto
        # de séries categóricas do skill dataviz).
        df_valido = df.copy()
        df_valido["_MES_ORD"] = pd.to_datetime(df_valido.get("MES_ANO_REF", ""), format="%m/%Y", errors="coerce")
        df_valido = df_valido.dropna(subset=["_MES_ORD"])
        if not df_valido.empty:
            agrupado = df_valido.groupby("MES_ANO_REF").agg(
                valor_total=("VALOR_TOTAL", "sum"),
                consumo_total=("CONSUMO_M3", "sum"),
                data_ordenacao=("_MES_ORD", "first"),
            )
            for mes_ano, linha in agrupado.iterrows():
                acumulado = serie_mensal_acumulador.setdefault(mes_ano, {
                    "mes_ano": mes_ano,
                    "data_ordenacao": linha["data_ordenacao"],
                    "valor_total": 0.0,
                    "consumo_total": 0.0,
                })
                acumulado["valor_total"] += float(linha["valor_total"] or 0)
                acumulado["consumo_total"] += float(linha["consumo_total"] or 0)

        if "CONSUMO_M3" in df.columns:
            for _, linha in df.nlargest(10, "CONSUMO_M3").iterrows():
                linhas_top_consumo.append({
                    "empresa": empresa,
                    "cliente": linha.get("NOME_CLIENTE") or "",
                    "mes_ano": linha.get("MES_ANO_REF") or "",
                    "consumo": float(linha.get("CONSUMO_M3") or 0),
                })
        if "VALOR_TOTAL" in df.columns:
            for _, linha in df.nlargest(10, "VALOR_TOTAL").iterrows():
                linhas_top_valor.append({
                    "empresa": empresa,
                    "cliente": linha.get("NOME_CLIENTE") or "",
                    "mes_ano": linha.get("MES_ANO_REF") or "",
                    "valor": float(linha.get("VALOR_TOTAL") or 0),
                })

    por_empresa.sort(key=lambda item: item["valor_total"], reverse=True)

    serie_mensal = sorted(serie_mensal_acumulador.values(), key=lambda item: item["data_ordenacao"])
    for item in serie_mensal:
        item.pop("data_ordenacao")
        item["valor_total"] = round(item["valor_total"], 2)
        item["consumo_total"] = round(item["consumo_total"], 2)

    top_consumo = sorted(linhas_top_consumo, key=lambda item: item["consumo"], reverse=True)[:10]
    top_valor = sorted(linhas_top_valor, key=lambda item: item["valor"], reverse=True)[:10]

    kpis["valor_total"] = round(kpis["valor_total"], 2)
    kpis["consumo_total"] = round(kpis["consumo_total"], 2)

    return {
        "kpis": kpis,
        "por_empresa": por_empresa,
        "serie_mensal": serie_mensal,
        "top_consumo": top_consumo,
        "top_valor": top_valor,
    }
