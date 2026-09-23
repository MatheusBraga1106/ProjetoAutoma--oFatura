"""Apoio dos testes da camada de persistência (nome único de propósito:
tests/ não é pacote e tests/upload tem seu próprio conftest).

Cada teste roda num SQLite temporário + storage temporário (pasta local, ou
S3 simulado com moto), com um contas.json FICTÍCIO. O texto do "PDF" é o
próprio conteúdo enviado (o pdftotext é trocado por uma cópia) e o OCR é
proibido — assim os testes não dependem de binário instalado. SANEAGO e
SANEAGO_ANALITICA usam os extratores REAIS sobre texto montado aqui; DEMAE
usa um extrator falso controlável (pra simular "correção de extrator")."""

import json
import os
import shutil
import sys

import pytest

RAIZ = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if RAIZ not in sys.path:
    sys.path.insert(0, RAIZ)

CONTAS_FICTICIAS = [
    {"CONTA": 123456, "CONTA_DV": "12345 6", "UNIDADE JUDICIÁRIA": "Fórum Teste", "ENDEREÇO": "Rua A",
     "DISTRIBUIDORA": "SANEAGO", "AGUA": True, "ESGOTO": True, "SMRSU": True},
    {"CONTA": 234567, "CONTA_DV": "23456 7", "UNIDADE JUDICIÁRIA": "Fórum Dois", "ENDEREÇO": "Rua B",
     "DISTRIBUIDORA": "SANEAGO", "AGUA": True, "ESGOTO": True, "SMRSU": True},
    {"CONTA": 9001, "CONTA_DV": "900 1", "UNIDADE JUDICIÁRIA": "Comarca Demae", "ENDEREÇO": "Rua C",
     "DISTRIBUIDORA": "DEMAE", "AGUA": True, "ESGOTO": False, "SMRSU": False},
]


class ExtratorFalso:
    """DEMAE falso: cada linha 'FATURA=..;MES=..;CONTA=..;AGUA=..;TOTAL=..'
    vira uma fatura. `bonus` soma no total (simula correção de extrator)."""

    def __init__(self):
        self.chamadas = 0
        self.bonus = 0.0
        self.ignorar_mes = False

    def __call__(self, caminho_txt, is_ocr):
        import pandas as pd

        self.chamadas += 1
        linhas = []
        with open(caminho_txt, encoding="utf-8") as f:
            for bruto in f:
                if not bruto.startswith("FATURA="):
                    continue
                campos = dict(p.split("=", 1) for p in bruto.strip().split(";"))
                total = float(campos["TOTAL"]) + self.bonus
                agua = float(campos.get("AGUA", total))
                linhas.append({
                    "CONCESSIONARIA": "DEMAE", "NUM_FATURA": campos["FATURA"],
                    "MES_ANO_REF": "" if self.ignorar_mes else campos.get("MES", ""),
                    "CONTA_DV": campos.get("CONTA", ""), "NOME_CLIENTE": campos.get("NOME", "CLIENTE"),
                    "CONSUMO_M3": 10.0, "VALOR_AGUA": agua, "VALOR_ESGOTO": 0.0, "VALOR_TAXAS_EXTRAS": 0.0,
                    "VALOR_OUTRAS_TAXAS": float(campos.get("OUTRAS", round(total - agua, 2))),
                    "VALOR_TOTAL": total,
                    "DATA_PROCESSAMENTO": "2026-01-01 00:00:00",
                })
        return pd.DataFrame(linhas) if linhas else None


def texto_demae(*linhas: str) -> bytes:
    cabecalho = "FATURA DE AGUA - DEMAE CALDAS NOVAS - documento de teste com texto suficiente\n"
    return (cabecalho + "\n".join(linhas) + "\n").encode("utf-8")


def texto_saneago(num_fatura: str, mes: str, contas: list[tuple[str, str, str, int, str, str, str]]) -> bytes:
    """Borderô SANEAGO no layout de texto nativo (colunas alinhadas à
    direita sob os rótulos ÁGUA/ESGOTO/SMRSU, como o pdftotext -layout)."""
    linhas = [
        "RELAÇÃO DE FATURAS - SANEAGO",
        f"Nº FATURA: {num_fatura}",
        f"MÊS/ANO REF: {mes}",
        "VENCIMENTO: 10/02/2024",
        f"{'CONTA - DV':<13}{'NOME CLIENTE':<22}{'LOGRADOURO':<20}{'CONSUMO':>8}{'ÁGUA':>10}{'ESGOTO':>10}{'SMRSU':>10}",
    ]
    for conta, nome, logradouro, consumo, agua, esgoto, smrsu in contas:
        linhas.append(f"{conta:<13}{nome:<22}{logradouro:<20}{consumo:>8}{agua:>10}{esgoto:>10}{smrsu:>10}")
    linhas.append("TOTAL GERAL")
    return ("\n".join(linhas) + "\n").encode("utf-8")


def texto_analitica(mes: str, contas: list[tuple[str, str]]) -> bytes:
    linhas = [f"ÓRGÃO PAGADOR: 2130 - TRIBUNAL DE TESTE   REFERÊNCIA: {mes}"]
    for conta_dv, hidrometro in contas:
        numero, dv = conta_dv.split()
        linhas.append(f"CONTA Nº {numero} - {dv}   ENDERECO QUALQUER")
        linhas.append(f"HIDRÔMETRO: {hidrometro} TIPO DE CONSUMO: NORMAL")
    return ("\n".join(linhas) + "\n").encode("utf-8")


def preparar_ambiente(tmp_path, monkeypatch):
    import banco.sessao
    import banco.storage
    import pipeline

    for var in ("DATABASE_URL", "S3_BUCKET", "S3_ENDPOINT_URL", "S3_ACCESS_KEY", "S3_SECRET_KEY", "S3_REGION"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DADOS_SAIDA_DIR", str(tmp_path / "saida"))
    monkeypatch.setenv("WORKER_EMBUTIDO", "0")
    monkeypatch.setenv("JOB_LEASE_SEGUNDOS", "90")
    caminho_contas = tmp_path / "contas.json"
    caminho_contas.write_text(json.dumps(CONTAS_FICTICIAS, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("CONTAS_JSON_PATH", str(caminho_contas))
    banco.sessao.reiniciar()
    banco.storage.reiniciar()

    # "PDF" de teste = texto puro; pdftotext vira cópia, OCR proibido.
    import extratores.ocr_fallback as ocr
    import extratores.pdftotext_fallback as p2t

    def pdftotext_falso(caminho_pdf, caminho_txt=None):
        shutil.copyfile(caminho_pdf, caminho_txt)
        return caminho_txt

    def ocr_proibido(*_a, **_k):
        raise AssertionError("OCR não deveria rodar nos testes")

    monkeypatch.setattr(p2t, "converter_pdf_para_txt", pdftotext_falso)
    monkeypatch.setattr(ocr, "gerar_txt_via_ocr", ocr_proibido)

    extrator = ExtratorFalso()
    versoes = {"DEMAE": "demae:v1"}
    monkeypatch.setitem(pipeline.EXTRATORES_POR_EMPRESA, "DEMAE", extrator)
    versao_real = pipeline.versao_extrator
    monkeypatch.setattr(pipeline, "versao_extrator", lambda emp: versoes.get(emp) or versao_real(emp))

    class Ambiente:
        pasta = tmp_path
        contas = caminho_contas
        demae = extrator
        versao = versoes

    return Ambiente


def enviar(arquivos: list[tuple[str, bytes]], parametros: "dict | None" = None) -> str:
    """O mesmo que o POST /pipeline/jobs faz, sem HTTP."""
    import ingestao
    from banco.sessao import sessao

    recebidos = [(ingestao.armazenar_blob(c), len(c), nome) for nome, c in arquivos]
    with sessao(escrita=True) as s:
        itens = []
        for sha, tamanho, nome in recebidos:
            arq, origem = ingestao.registrar_arquivo(s, sha, tamanho, nome)
            itens.append((arq, origem, nome))
        return ingestao.criar_job(s, itens, parametros=parametros).id


def rodar(job_id: str) -> dict:
    import worker
    from banco.modelos import Job
    from banco.sessao import sessao

    wid = worker.novo_worker_id()
    assert worker.pegar_proximo_job(wid, job_id=job_id) == job_id
    assert worker.executar_job(job_id, wid) == "concluido"
    with sessao() as s:
        return s.get(Job, job_id).resultado


def faturas_ativas(empresa: "str | None" = None) -> list:
    from sqlalchemy import select

    from banco.modelos import Fatura
    from banco.sessao import sessao

    with sessao() as s:
        q = select(Fatura).where(Fatura.ativa.is_(True)).order_by(Fatura.id)
        if empresa:
            q = q.where(Fatura.empresa == empresa)
        return list(s.scalars(q))


def contar(modelo, *condicoes) -> int:
    from sqlalchemy import func, select

    from banco.sessao import sessao

    with sessao() as s:
        return s.scalar(select(func.count()).select_from(modelo).where(*condicoes))
