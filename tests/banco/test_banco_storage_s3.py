"""Storage S3 (moto) + migração idempotente + importação dos erros antigos."""

import sqlite3

import boto3
import pytest
from apoio_banco import contar, enviar, faturas_ativas, rodar, texto_demae
from moto import mock_aws

import banco.storage
from banco.modelos import ArquivoUpload, ErroReportado


@pytest.fixture
def s3(ambiente, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "teste")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "teste")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        monkeypatch.setenv("S3_BUCKET", "faturas-teste")
        monkeypatch.setenv("S3_ACCESS_KEY", "teste")
        monkeypatch.setenv("S3_SECRET_KEY", "teste")
        monkeypatch.setenv("S3_REGION", "us-east-1")
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="faturas-teste")
        banco.storage.reiniciar()
        yield boto3.client("s3", region_name="us-east-1")
        banco.storage.reiniciar()


def test_dedup_e_cache_de_texto_no_s3(s3, ambiente):
    pdf = texto_demae("FATURA=1;MES=01/2024;CONTA=900-1;TOTAL=5.00")
    rodar(enviar([("demae/a.pdf", pdf), ("demae/b.pdf", pdf)]))
    rodar(enviar([("demae/c.pdf", pdf)]))
    chaves = [o["Key"] for o in s3.list_objects_v2(Bucket="faturas-teste")["Contents"]]
    assert sorted(k.split("/")[0] for k in chaves) == ["pdfs", "textos"]  # 1 blob + 1 texto em cache
    assert contar(ArquivoUpload) == 1 and len(faturas_ativas()) == 1


def test_bucket_inexistente_da_erro_claro_e_nao_cria(s3, monkeypatch):
    monkeypatch.setenv("S3_BUCKET", "nao-existe")
    banco.storage.reiniciar()
    with pytest.raises(RuntimeError, match="não cria bucket"):
        banco.storage.obter_armazenamento().garantir_bucket()
    assert "nao-existe" not in [b["Name"] for b in s3.list_buckets()["Buckets"]]


def test_prefixo_do_storage_como_entrada_da_reextracao(s3, ambiente):
    import ingestao

    pdf = texto_demae("FATURA=1;MES=01/2024;CONTA=900-1;TOTAL=5.00")
    s3.put_object(Bucket="faturas-teste", Key="entrada/dados_entrada/AGUA - DEMAE/2024/x.pdf", Body=pdf)
    s3.put_object(Bucket="faturas-teste", Key="entrada/dados_entrada/AGUA - DEMAE/2024/x.txt", Body=pdf)
    job_id = ingestao.job_de_prefixo_storage("entrada/")
    rodar(job_id)
    (f,) = faturas_ativas()
    assert f.dados["PASTA_ORIGEM"] == "dados_entrada/AGUA - DEMAE/2024"


def test_migracao_idempotente_e_importacao_de_erros(ambiente, tmp_path):
    from banco.migrar import importar_erros_sqlite, main

    assert main([]) == 0 and main([]) == 0
    antigo = tmp_path / "erros_antigo.db"
    c = sqlite3.connect(antigo)
    c.execute("CREATE TABLE erros_reportados (id INTEGER PRIMARY KEY, concessionaria TEXT, num_fatura TEXT, "
              "conta_dv TEXT, mes_ano_ref TEXT, mensagem TEXT, status TEXT, data_criacao TEXT)")
    c.execute("INSERT INTO erros_reportados VALUES (1,'DEMAE','1','900-1','01/2024','msg','aberto','2026-09-22 10:00:00')")
    c.commit()
    c.close()
    assert importar_erros_sqlite(str(antigo)) == 1
    assert importar_erros_sqlite(str(antigo)) == 0
    assert contar(ErroReportado) == 1
