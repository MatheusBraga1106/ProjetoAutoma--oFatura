import asyncio
import json
import os
import shutil
import sys
import tempfile
import uuid
from contextlib import asynccontextmanager
from datetime import timezone
from typing import List

import pandas as pd
from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import func, select

# Os extratores originais imprimem emojis de debug (✅❌🔎...) via print().
# Num console/ambiente cujo stdout não seja UTF-8 (comum no Windows, e
# depende do locale da imagem base no Docker), isso derruba a extração
# inteira com UnicodeEncodeError antes mesmo de chegar no try/except do
# endpoint. Forçar UTF-8 aqui evita mexer nos prints de cada extrator.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import ingestao  # noqa: E402
from banco.config import caminho_contas_json, pasta_saida, worker_embutido  # noqa: E402
from banco.modelos import Fatura, Job, JobEvento  # noqa: E402
from banco.sessao import obter_engine, sessao  # noqa: E402
from banco.storage import obter_armazenamento  # noqa: E402
from erros_reportados import atualizar_status, criar_erro, listar_erros  # noqa: E402
from extratores import ocr_fallback  # noqa: E402
from extratores.pdftotext_fallback import PDFTOTEXT_CMD  # noqa: E402
from pipeline import (  # noqa: E402
    EMPRESAS_CONHECIDAS,
    EXTRATORES_NAO_IMPLEMENTADOS,
    EXTRATORES_POR_EMPRESA,
    identificar_distribuidora,
    linha_para_dados,
    mesclar_saneago_com_analitica,
)


@asynccontextmanager
async def ciclo_de_vida(_app: FastAPI):
    # WORKER_EMBUTIDO (default quando o banco é SQLite, i.e. dev): a própria
    # API processa a fila numa thread — inclusive jobs que ficaram pela
    # metade num restart. Em produção (Postgres) quem processa é o
    # `python worker.py`, e isto fica desligado.
    # Storage: só confere (head_bucket) — a credencial da app não pode criar
    # bucket. Falha não derruba a API (a UI/health continuam no ar), mas vai
    # pro log e pro /pipeline/status-sistema.
    _app.state.erro_armazenamento = None
    try:
        obter_armazenamento().garantir_bucket()
    except Exception as e:  # noqa: BLE001
        _app.state.erro_armazenamento = str(e)
        print(f"[api] ERRO no storage: {e}", file=sys.stderr, flush=True)

    parar = None
    if worker_embutido():
        import worker

        _thread, parar = worker.iniciar_embutido()
    yield
    if parar is not None:
        parar.set()


app = FastAPI(title="Extrator de Faturas de Água", lifespan=ciclo_de_vida)


@app.middleware("http")
async def estaticos_sempre_revalidados(request: Request, call_next):
    # Sem Cache-Control o navegador aplica cache heurístico e segue servindo
    # JS/CSS antigos depois de uma atualização. "no-cache" força revalidar
    # (304 barato via ETag quando nada mudou).
    resposta = await call_next(request)
    if request.url.path.startswith("/static/"):
        resposta.headers["Cache-Control"] = "no-cache"
    return resposta

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
        "VALOR_AGUA", "VALOR_ESGOTO", "VALOR_TAXAS_EXTRAS", "VALOR_OUTRAS_TAXAS", "VALOR_TOTAL",
    ],
}


def _validar_empresa(empresa: str) -> str:
    """Whitelist contra path traversal — "empresa" costuma vir direto da URL."""
    empresa_upper = empresa.upper()
    if empresa_upper not in EMPRESAS_CONHECIDAS:
        raise HTTPException(status_code=404, detail="Empresa desconhecida.")
    return empresa_upper


def _texto_ou_vazio(valor) -> str:
    """`valor or ""` não pega NaN (é truthy em Python) — vira float('nan') no
    dict e quebra o json.dumps da resposta. Usa isso pra qualquer campo de
    texto que pode vir vazio."""
    return "" if pd.isna(valor) else str(valor)


def _verdadeiro(valor: "str | None") -> bool:
    return (valor or "").strip().lower() in ("1", "true", "sim", "yes", "on")


def _resposta_csv(conteudo: bytes, empresa: str) -> Response:
    nome = f"banco_dados_{empresa.lower()}.csv"
    return Response(content=conteudo, media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{nome}"'})


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

            # Nunca usar o nome recebido como caminho: pode ter "/" ou "..".
            # Uma subpasta opaca por arquivo + nome saneado (o extrator da
            # analítica lê o mês/ano do NOME do arquivo quando o texto não tem).
            subpasta = os.path.join(pasta_tmp, uuid.uuid4().hex)
            os.makedirs(subpasta)
            caminho_pdf = os.path.join(subpasta, ingestao.nome_seguro(arquivo.filename or "arquivo") + ".pdf")
            with open(caminho_pdf, "wb") as f:
                f.write(_ler_com_limite(arquivo))

            try:
                caminho_txt, veio_de_ocr = ingestao.extrair_texto_de_pdf(caminho_pdf)
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
            for nome, df in lotes_analitica:
                for _, linha in df.iterrows():
                    resultados.append({
                        "arquivo_origem": nome,
                        "status": "ok",
                        "dados": linha_para_dados(linha),
                    })

    return {"resultados": resultados}


# =========================================================
# PIPELINE COMPLETO — upload vira job persistido (tabela jobs); quem
# processa é o worker (worker.py, ou a thread embutida em dev). O SSE lê os
# eventos gravados no banco, então sobrevive a restart e funciona com a API
# e o worker em processos/containers diferentes.
# =========================================================

def _comando_resolvido(cmd: str) -> bool:
    if os.path.isabs(cmd):
        return os.path.exists(cmd)
    return shutil.which(cmd) is not None


@app.get("/pipeline/status-sistema")
def status_sistema():
    armazenamento = obter_armazenamento()
    with sessao() as s:
        pendentes = s.scalar(select(func.count()).select_from(Job).where(Job.status.in_(("pendente", "processando"))))
    return {
        "contas_json_encontrado": os.path.exists(caminho_contas_json()),
        # Mesma resolução que o OCR usa (env -> PATH -> instalação padrão no
        # Windows); o tesseract_cmd do pytesseract só é ajustado quando
        # ocr_fallback é importado, o que pode ainda não ter acontecido.
        "tesseract_resolvido": _comando_resolvido(ocr_fallback.localizar_tesseract()),
        "pdftotext_resolvido": _comando_resolvido(PDFTOTEXT_CMD),
        "distribuidoras_implementadas": sorted(EXTRATORES_POR_EMPRESA),
        "distribuidoras_nao_implementadas": sorted(EXTRATORES_NAO_IMPLEMENTADOS),
        "pasta_saida": pasta_saida(),
        "banco": obter_engine().dialect.name,
        "armazenamento": armazenamento.tipo,
        "armazenamento_erro": getattr(app.state, "erro_armazenamento", None),
        "worker_embutido": worker_embutido(),
        "jobs_na_fila": int(pendentes or 0),
    }


# Maior fatura real hoje: 26 MB. Sem teto, um upload de 300 MB entrava
# inteiro na memória (medido nos testes de upload).
UPLOAD_MAX_BYTES = int(float(os.environ.get("UPLOAD_MAX_MB", "100")) * 1024 * 1024)


def _ler_com_limite(arquivo: UploadFile) -> bytes:
    partes, total = [], 0
    while bloco := arquivo.file.read(1024 * 1024):
        total += len(bloco)
        if total > UPLOAD_MAX_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"{arquivo.filename}: arquivo maior que o limite de {UPLOAD_MAX_BYTES // (1024 * 1024)} MB.",
            )
        partes.append(bloco)
    return b"".join(partes)


@app.post("/pipeline/jobs")
def criar_job(background_tasks: BackgroundTasks, arquivos: List[UploadFile] = File(...)):
    if not arquivos:
        raise HTTPException(status_code=400, detail="Nenhum arquivo enviado.")

    arquivos_pdf = [a for a in arquivos if (a.filename or "").lower().endswith(".pdf")]
    if not arquivos_pdf:
        raise HTTPException(status_code=400, detail="Nenhum PDF encontrado no envio.")

    # Blob primeiro (fora da transação; endereçado por conteúdo, idempotente),
    # depois uma transação curta registrando arquivos/origens/job.
    armazenamento = obter_armazenamento()
    recebidos = []
    for arquivo in arquivos_pdf:
        conteudo = _ler_com_limite(arquivo)
        sha = ingestao.armazenar_blob(conteudo, armazenamento)
        recebidos.append((sha, len(conteudo), arquivo.filename or "arquivo.pdf"))

    with sessao(escrita=True) as s:
        itens = []
        for sha, tamanho, nome in recebidos:
            arq, origem = ingestao.registrar_arquivo(s, sha, tamanho, nome)
            itens.append((arq, origem, nome))
        job = ingestao.criar_job(s, itens)
        job_id, total = job.id, job.total_arquivos

    if worker_embutido():
        import worker

        background_tasks.add_task(worker.executar_job_se_disponivel, job_id)

    return {"job_id": job_id, "total_arquivos": total}


def _obter_job(job_id: str) -> Job:
    with sessao() as s:
        job = s.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job não encontrado.")
    return job


@app.get("/pipeline/jobs/{job_id}")
def status_job(job_id: str):
    job = _obter_job(job_id)
    return {
        "job_id": job.id,
        "status": job.status,
        "total_arquivos": job.total_arquivos,
        "resultado": job.resultado,
    }


def _eventos_desde(job_id: str, ultimo_id: int) -> tuple[list[tuple[int, str, dict]], "str | None"]:
    with sessao() as s:
        eventos = [(e.id, e.tipo, e.dados) for e in s.scalars(
            select(JobEvento).where(JobEvento.job_id == job_id, JobEvento.id > ultimo_id).order_by(JobEvento.id))]
        status = s.scalar(select(Job.status).where(Job.id == job_id))
    return eventos, status


@app.get("/pipeline/jobs/{job_id}/eventos")
async def eventos_job(job_id: str):
    _obter_job(job_id)

    async def streamer():
        ultimo_id = 0
        while True:
            eventos, status = await asyncio.to_thread(_eventos_desde, job_id, ultimo_id)
            for id_evento, tipo, dados in eventos:
                ultimo_id = id_evento
                yield f"event: {tipo}\ndata: {json.dumps(dados, ensure_ascii=False)}\n\n"
                if tipo in ("concluido", "erro"):
                    return
            if status in ("concluido", "erro") and not eventos:
                # Terminal sem evento terminal (não deveria acontecer): fecha
                # do mesmo jeito que antes, pra UI não ficar pendurada.
                return
            await asyncio.sleep(0.4)

    return StreamingResponse(streamer(), media_type="text/event-stream")


@app.get("/pipeline/jobs/{job_id}/csv/{empresa}")
def baixar_csv(job_id: str, empresa: str):
    _obter_job(job_id)
    empresa = _validar_empresa(empresa)
    with sessao() as s:
        conteudo = ingestao.exportar_csv_empresa(s, empresa)
    if conteudo is None:
        raise HTTPException(status_code=404, detail="CSV não encontrado para essa empresa.")
    return _resposta_csv(conteudo, empresa)


# =========================================================
# DADOS CONSOLIDADOS (aba "Dados" da UI) — faturas ativas do banco, por
# distribuidora, com busca e paginação no SQL.
# =========================================================

def _listar_empresas_com_dados(s) -> list[dict]:
    linhas = {emp: (n, ult) for emp, n, ult in s.execute(
        select(Fatura.empresa, func.count(), func.max(Fatura.atualizado_em))
        .where(Fatura.ativa.is_(True)).group_by(Fatura.empresa))}
    empresas = []
    for empresa in EMPRESAS_CONHECIDAS:
        if empresa not in linhas:
            continue
        n, ultima = linhas[empresa]
        if ultima is not None:
            ultima = ultima.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
        empresas.append({
            "empresa": empresa,
            "linhas": int(n),
            "ultima_modificacao": ultima.isoformat(timespec="seconds") if ultima else None,
        })
    return empresas


@app.get("/dados/empresas")
def dados_empresas():
    with sessao() as s:
        return {"empresas": _listar_empresas_com_dados(s)}


@app.get("/dados/{empresa}")
def dados_empresa(empresa: str, pagina: int = 1, tamanho_pagina: int = 50, busca: str = "",
                  somente_suspeitas: str = ""):
    empresa = _validar_empresa(empresa)
    pagina = max(pagina, 1)
    tamanho_pagina = min(max(tamanho_pagina, 1), 500)

    with sessao() as s:
        filtros = [Fatura.empresa == empresa, Fatura.ativa.is_(True)]
        if not s.scalar(select(func.count()).select_from(Fatura).where(*filtros)):
            raise HTTPException(status_code=404, detail="Nenhum dado encontrado para essa empresa.")
        if busca.strip():
            filtros.append(Fatura.texto_busca.contains(busca.strip().lower(), autoescape=True))
        if _verdadeiro(somente_suspeitas):
            filtros.append(Fatura.suspeito.is_(True))

        total = s.scalar(select(func.count()).select_from(Fatura).where(*filtros))
        linhas = [d for (d,) in s.execute(
            select(Fatura.dados).where(*filtros)
            .order_by(Fatura.competencia.desc().nulls_last(), Fatura.id)
            .offset((pagina - 1) * tamanho_pagina).limit(tamanho_pagina))]

    return {
        "empresa": empresa,
        "total": int(total),
        "pagina": pagina,
        "tamanho_pagina": tamanho_pagina,
        "linhas": linhas,
    }


@app.get("/dados/{empresa}/csv")
def dados_empresa_csv(empresa: str):
    empresa = _validar_empresa(empresa)
    with sessao() as s:
        conteudo = ingestao.exportar_csv_empresa(s, empresa)
    if conteudo is None:
        raise HTTPException(status_code=404, detail="CSV não encontrado para essa empresa.")
    return _resposta_csv(conteudo, empresa)


# =========================================================
# DASHBOARD (aba "Dashboards" da UI) — visão agregada de todas as
# distribuidoras: KPIs, comparação por empresa, série mensal e maiores
# consumos/valores. Mesmo cálculo de antes, com o DataFrame vindo do banco.
# =========================================================

@app.get("/dashboard/resumo")
def dashboard_resumo(empresa: "str | None" = None):
    """KPIs e comparação por empresa sempre agregam todas as distribuidoras
    (são gráficos de comparação — não faz sentido filtrar). Já a série
    mensal e os "maiores consumos/valores" respeitam o filtro `empresa`,
    quando informado."""
    empresa_filtro = _validar_empresa(empresa) if empresa else None

    kpis = {"total_faturas": 0, "valor_total": 0.0, "consumo_total": 0.0, "total_suspeitas": 0}
    por_empresa = []
    serie_mensal_acumulador: dict = {}
    linhas_top_consumo = []
    linhas_top_valor = []

    with sessao() as s:
        empresas_info = _listar_empresas_com_dados(s)
        dataframes = [(info["empresa"], ingestao.dataframe_empresa(s, info["empresa"])) for info in empresas_info]

    for empresa, df in dataframes:
        if df is None or df.empty:
            continue
        for col in ("VALOR_TOTAL", "CONSUMO_M3"):
            if col not in df.columns:
                df[col] = 0.0
        if "SUSPEITO" not in df.columns:
            df["SUSPEITO"] = False

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

        if empresa_filtro is not None and empresa != empresa_filtro:
            continue

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

        for _, linha in df.nlargest(10, "CONSUMO_M3").iterrows():
            linhas_top_consumo.append({
                "empresa": empresa,
                "cliente": _texto_ou_vazio(linha.get("NOME_CLIENTE")),
                "mes_ano": _texto_ou_vazio(linha.get("MES_ANO_REF")),
                "consumo": float(linha.get("CONSUMO_M3") or 0),
            })
        for _, linha in df.nlargest(10, "VALOR_TOTAL").iterrows():
            linhas_top_valor.append({
                "empresa": empresa,
                "cliente": _texto_ou_vazio(linha.get("NOME_CLIENTE")),
                "mes_ano": _texto_ou_vazio(linha.get("MES_ANO_REF")),
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


# =========================================================
# ERROS REPORTADOS (aba "Erros" da UI) — fila de revisão manual, agora na
# tabela erros_reportados do mesmo banco. Não altera fatura nenhuma.
# =========================================================

class NovoErro(BaseModel):
    mensagem: str = Field(..., min_length=1, max_length=2000)
    concessionaria: str = ""
    num_fatura: str = ""
    conta_dv: str = ""
    mes_ano_ref: str = ""


class AtualizarStatusErro(BaseModel):
    status: str


@app.get("/erros")
def erros_listar(status: "str | None" = None):
    if status and status not in ("aberto", "resolvido"):
        raise HTTPException(status_code=400, detail="status deve ser 'aberto' ou 'resolvido'.")
    return {"erros": listar_erros(status)}


@app.post("/erros")
def erros_criar(novo: NovoErro):
    mensagem = novo.mensagem.strip()
    if not mensagem:
        raise HTTPException(status_code=400, detail="mensagem não pode ser vazia.")
    return criar_erro(
        mensagem=mensagem,
        concessionaria=novo.concessionaria,
        num_fatura=novo.num_fatura,
        conta_dv=novo.conta_dv,
        mes_ano_ref=novo.mes_ano_ref,
    )


@app.patch("/erros/{erro_id}")
def erros_atualizar_status(erro_id: int, corpo: AtualizarStatusErro):
    try:
        atualizado = atualizar_status(erro_id, corpo.status)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if atualizado is None:
        raise HTTPException(status_code=404, detail="Erro reportado não encontrado.")
    return atualizado
