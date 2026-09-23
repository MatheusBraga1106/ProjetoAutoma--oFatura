"""Ponta a ponta com faturas REAIS (somente leitura de dados_entrada/,
contas.json e dados_saida/): ingere uma amostra por distribuidora num SQLite
temporário, com os extratores reais, e compara contagem/total/suspeitas com
as linhas do CSV atual produzidas pelo Main.py para os mesmos arquivos.
Pulado se os dados reais não estiverem presentes. Não imprime conteúdo."""

import glob
import os
import unicodedata

import pandas as pd
import pytest

RAIZ = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ENTRADA = os.path.join(RAIZ, "dados_entrada")
# CSVs de referência (saída do Main.py antigo). Desde a migração pro banco
# eles não ficam mais em dados_saida/: extraia o backup e aponte pra pasta,
# ex.: BASELINE_CSV_DIR=<pasta>/dados_saida
SAIDA = os.environ.get("BASELINE_CSV_DIR") or os.path.join(RAIZ, "dados_saida")
CONTAS = os.path.join(RAIZ, "contas.json")
POR_EMPRESA = int(os.environ.get("AMOSTRA_POR_DISTRIBUIDORA", "4"))

pytestmark = pytest.mark.skipif(
    not (os.path.isdir(ENTRADA) and os.path.exists(CONTAS)
         and glob.glob(os.path.join(SAIDA, "banco_dados_*.csv"))),
    reason="dados reais ou CSVs de referência ausentes (defina BASELINE_CSV_DIR)",
)


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s).replace("\\", "/")


@pytest.fixture
def banco_real(tmp_path, monkeypatch):
    import banco.sessao
    import banco.storage

    for var in ("DATABASE_URL", "S3_BUCKET", "S3_ENDPOINT_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DADOS_SAIDA_DIR", str(tmp_path / "saida"))
    monkeypatch.setenv("CONTAS_JSON_PATH", CONTAS)
    monkeypatch.setenv("WORKER_EMBUTIDO", "0")
    banco.sessao.reiniciar()
    banco.storage.reiniciar()
    yield
    banco.sessao.reiniciar()
    banco.storage.reiniciar()


def test_amostra_real_bate_com_csv_atual(banco_real):
    import ingestao
    import pipeline
    import worker
    from sqlalchemy import select

    from banco.modelos import Fatura, FaturaOrigem, JobArquivo
    from banco.sessao import sessao

    # Amostra: os N primeiros PDFs (ordem de caminho) de cada distribuidora implementada.
    escolhidos, contagem = [], {}
    for completo, caminho in ingestao.listar_pdfs_pasta(ENTRADA):
        emp = pipeline.identificar_distribuidora(caminho)
        if emp is None or emp in pipeline.EXTRATORES_NAO_IMPLEMENTADOS or contagem.get(emp, 0) >= POR_EMPRESA:
            continue
        contagem[emp] = contagem.get(emp, 0) + 1
        escolhidos.append((completo, caminho))
    assert len(contagem) >= 9

    def fontes():
        for completo, caminho in escolhidos:
            yield caminho, (lambda p=completo: open(p, "rb").read()), (lambda p=completo: ingestao._texto_irmao_util(p))

    job_id = ingestao.job_de_arquivos(fontes(), usar_txt_existente=True, parametros={})
    wid = worker.novo_worker_id()
    assert worker.pegar_proximo_job(wid, job_id=job_id) == job_id
    assert worker.executar_job(job_id, wid) == "concluido"

    with sessao() as s:
        itens = {it.caminho_origem: it for it in s.scalars(select(JobArquivo).where(JobArquivo.job_id == job_id))}
        diferencas, comparadas = [], 0
        for emp in sorted(contagem):
            if emp == "SANEAGO_ANALITICA":
                continue
            caminhos = [c for _, c in escolhidos if pipeline.identificar_distribuidora(c) == emp]
            # Linhas do CSV (só as do Main.py, que lê o .txt — ARQUIVO_ORIGEM termina em .txt)
            csv = pd.read_csv(os.path.join(SAIDA, f"banco_dados_{emp.lower()}.csv"), sep=";", dtype=str,
                              encoding="utf-8-sig", keep_default_na=False)
            csv = csv[csv["ARQUIVO_ORIGEM"].str.lower().str.endswith(".txt")]
            alvo = {(_nfc(os.path.dirname(c)).split("dados_entrada/", 1)[-1],
                     os.path.splitext(_nfc(os.path.basename(c)))[0]) for c in caminhos}
            chave_csv = [(_nfc(p).split("dados_entrada/", 1)[-1], os.path.splitext(_nfc(a))[0])
                         for p, a in zip(csv["PASTA_ORIGEM"], csv["ARQUIVO_ORIGEM"])]
            linhas_csv = csv[[k in alvo for k in chave_csv]].to_dict("records")
            unicas, vistos = [], set()
            for i, r in enumerate(linhas_csv):
                nf = ingestao.normalizar_num_fatura(r["NUM_FATURA"])
                mes = ingestao.normalizar_mes_ano(r["MES_ANO_REF"])
                conta = pipeline.normalizar_conta_dv(r["CONTA_DV"])
                k = f"{nf}|{mes}|{conta}" if ingestao.chave_eh_completa(emp, nf, mes, conta) else f"inc{i}"
                if k not in vistos:
                    vistos.add(k)
                    unicas.append(r)

            ids_arquivos = [itens[c].arquivo_id for c in caminhos]
            faturas = {f.id: f for f in s.scalars(
                select(Fatura).join(FaturaOrigem, FaturaOrigem.fatura_id == Fatura.id)
                .where(FaturaOrigem.arquivo_id.in_(ids_arquivos), Fatura.ativa.is_(True)))}.values()

            n_db, n_csv = len(faturas), len(unicas)
            total_db = round(sum(f.valor_total or 0 for f in faturas), 2)
            total_csv = round(sum(float(r["VALOR_TOTAL"] or 0) for r in unicas), 2)
            sus_db = sum(1 for f in faturas if f.suspeito)
            sus_csv = sum(1 for r in unicas if r["SUSPEITO"] == "True")
            comparadas += n_csv
            if (n_db, total_db, sus_db) != (n_csv, total_csv, sus_csv):
                diferencas.append((emp, (n_db, total_db, sus_db), (n_csv, total_csv, sus_csv)))
        assert not diferencas, f"(empresa, banco, csv): {diferencas}"
        assert comparadas > 50  # a amostra casou de fato com linhas do CSV
