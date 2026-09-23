"""Dedup de ARQUIVO (sha256) e de FATURA (chave natural), incluindo chave incompleta."""

from apoio_banco import contar, enviar, faturas_ativas, rodar, texto_demae, texto_saneago

from banco.modelos import ArquivoOrigem, ArquivoUpload, Fatura, FaturaVersao
from banco.storage import obter_armazenamento

PDF_A = texto_demae("FATURA=1001;MES=01/2024;CONTA=900-1;TOTAL=50.00",
                    "FATURA=1002;MES=02/2024;CONTA=900-1;TOTAL=60.00")


def test_mesmo_arquivo_no_mesmo_lote_e_em_outro_lote_nao_duplica(ambiente):
    job1 = enviar([("demae/2024/a.pdf", PDF_A), ("demae/2024/a.pdf", PDF_A)])
    r1 = rodar(job1)
    assert contar(ArquivoUpload) == 1
    assert len(faturas_ativas()) == 2
    assert ambiente.demae.chamadas == 1  # o 2º item do lote é pulado (mesmo conteúdo, mesma versão)
    assert r1["empresas"]["DEMAE"]["adicionadas"] == 2
    assert r1["empresas"]["DEMAE"]["duplicadas"] == 2

    job2 = enviar([("demae/2024/a.pdf", PDF_A)])
    r2 = rodar(job2)
    assert ambiente.demae.chamadas == 1  # reenviar sem mudar código não gera trabalho
    assert r2["empresas"]["DEMAE"] == {**r2["empresas"]["DEMAE"], "adicionadas": 0, "atualizadas": 0, "duplicadas": 2}
    assert len(faturas_ativas()) == 2
    assert contar(FaturaVersao) == 2

    pdfs = obter_armazenamento().listar("pdfs/")
    assert len(pdfs) == 1  # um blob só


def test_mesmo_conteudo_por_caminhos_diferentes_guarda_todas_as_origens(ambiente):
    # O 2º caminho não identifica distribuidora nenhuma ("215449 (1).pdf"),
    # mas o conteúdo já é conhecido como DEMAE pela 1ª origem.
    rodar(enviar([("AGUA - DEMAE CALDAS NOVAS/2024/215449.pdf", PDF_A)]))
    r = rodar(enviar([("215449 (1).pdf", PDF_A), ("outra pasta/copia.pdf", PDF_A)]))
    assert contar(ArquivoUpload) == 1
    assert contar(ArquivoOrigem) == 3
    assert all(a["status"] == "ok" for a in r["arquivos"]), r["arquivos"]
    assert len(faturas_ativas()) == 2
    # ARQUIVO_ORIGEM exibido continua sendo o da origem que identificou a distribuidora
    assert {f.dados["ARQUIVO_ORIGEM"] for f in faturas_ativas()} == {"215449.pdf"}


def test_arquivo_sem_distribuidora_identificavel_vira_erro(ambiente):
    r = rodar(enviar([("sem pista nenhuma.pdf", PDF_A)]))
    assert r["arquivos"][0]["status"] == "erro"
    assert "identificar" in r["arquivos"][0]["erro"]
    assert contar(Fatura) == 0


def test_mesma_fatura_em_dois_arquivos_diferentes_e_uma_fatura_so(ambiente):
    outro = texto_demae("FATURA=1001;MES=01/2024;CONTA=0900 1;TOTAL=50.00")  # conta em outro formato
    rodar(enviar([("demae/a.pdf", PDF_A), ("demae/segunda via.pdf", outro)]))
    assert len(faturas_ativas()) == 2
    assert contar(ArquivoUpload) == 2


def test_chave_incompleta_nao_se_perde_nem_funde_com_outro_arquivo(ambiente):
    # Linha só com a conta (típico do canhoto lido por OCR): sem NUM_FATURA e sem mês.
    arq1 = texto_demae("FATURA=;MES=;CONTA=900-1;TOTAL=50.00")
    arq2 = texto_demae("FATURA=;MES=;CONTA=900-1;TOTAL=50.00", "  ")
    rodar(enviar([("demae/x1.pdf", arq1), ("demae/x2.pdf", arq2)]))
    ativas = faturas_ativas()
    assert len(ativas) == 2  # uma por arquivo — nunca fundidas em silêncio
    assert all(not f.chave_completa and f.dados["ALERTA_CHAVE"] == "incompleta" for f in ativas)
    # reenviar o mesmo arquivo não duplica
    rodar(enviar([("demae/x1 de novo.pdf", arq1)], parametros={"forcar": True}))
    assert len(faturas_ativas()) == 2


def test_chave_repetida_dentro_do_mesmo_arquivo_mantem_as_duas(ambiente):
    arq = texto_demae("FATURA=7;MES=03/2024;CONTA=900-1;TOTAL=10.00",
                      "FATURA=7;MES=03/2024;CONTA=900-1;TOTAL=10.00")
    rodar(enviar([("demae/rep.pdf", arq)]))
    ativas = faturas_ativas()
    assert len(ativas) == 2
    assert sorted(f.dados["ALERTA_CHAVE"] for f in ativas) == ["", "repetida_no_arquivo"]


def test_saneago_num_fatura_cobre_varias_contas(ambiente):
    contas = [("12345 6", "FORUM TESTE", "RUA A 1", 10, "100,00", "50,00", "5,00"),
              ("23456 7", "FORUM DOIS", "RUA B 2", 20, "200,00", "80,00", "7,00")]
    rodar(enviar([("Faturas-Saneago-Borderô/2024/jan.pdf", texto_saneago("555001", "01/2024", contas))]))
    ativas = faturas_ativas("SANEAGO")
    assert len(ativas) == 2
    assert {f.num_fatura for f in ativas} == {"555001"}
    assert {f.conta_normalizada for f in ativas} == {"123456", "234567"}
    assert sorted(f.valor_total for f in ativas) == [155.0, 287.0]
    assert all(f.dados["UNIDADE JUDICIÁRIA"] for f in ativas)  # enriquecido com contas.json
