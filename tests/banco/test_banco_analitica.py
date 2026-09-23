"""Cruzamento SANEAGO + ANALÍTICA entre lotes (analítica antes ou depois),
com os extratores reais sobre texto montado."""

from apoio_banco import enviar, faturas_ativas, rodar, texto_analitica, texto_saneago

CONTAS = [("12345 6", "FORUM TESTE", "RUA A 1", 10, "100,00", "50,00", "5,00"),
          ("23456 7", "FORUM DOIS", "RUA B 2", 20, "200,00", "80,00", "7,00")]
BORDERO_JAN = ("Faturas-Saneago-Borderô/2024/FATURA SANEAGO - JANEIRO.pdf", texto_saneago("555001", "01/2024", CONTAS))
BORDERO_FEV = ("Faturas-Saneago-Borderô/2024/FATURA SANEAGO - FEVEREIRO.pdf", texto_saneago("555002", "02/2024", CONTAS))
ANALITICA_JAN = ("Faturas-Saneago-Analítica/2024/analitica_01_2024.pdf",
                 texto_analitica("01/2024", [("12345 6", "HJAN1"), ("23456 7", "HJAN2")]))


def _hidrometros():
    return {(f.conta_normalizada, f.mes_ano_ref): f.dados.get("NUM_HIDROMETRO") for f in faturas_ativas("SANEAGO")}


def test_analitica_chega_depois_da_fatura(ambiente):
    rodar(enviar([BORDERO_JAN]))
    assert set(_hidrometros().values()) == {""}
    r = rodar(enviar([ANALITICA_JAN]))
    assert "SANEAGO_ANALITICA" not in r["empresas"]  # analítica não é tabela própria, igual antes
    assert _hidrometros() == {("123456", "01/2024"): "HJAN1", ("234567", "01/2024"): "HJAN2"}


def test_analitica_chega_antes_da_fatura_e_fallback_mais_recente(ambiente):
    rodar(enviar([ANALITICA_JAN]))
    rodar(enviar([BORDERO_JAN, BORDERO_FEV]))
    h = _hidrometros()
    assert h[("123456", "01/2024")] == "HJAN1"
    assert h[("123456", "02/2024")] == "HJAN1"  # sem analítica de fevereiro: hidrômetro mais recente da conta

    # Chega a analítica de fevereiro com hidrômetro trocado: só fevereiro muda.
    rodar(enviar([("Faturas-Saneago-Analítica/2024/analitica_02_2024.pdf",
                   texto_analitica("02/2024", [("12345 6", "HFEV1")]))]))
    h = _hidrometros()
    assert h[("123456", "01/2024")] == "HJAN1"
    assert h[("123456", "02/2024")] == "HFEV1"
    assert h[("234567", "02/2024")] == "HJAN2"


def test_analitica_sem_referencia_usa_mes_do_nome_do_arquivo(ambiente):
    # O extrator lê o mês do NOME do arquivo quando o texto não tem REFERÊNCIA:
    # por isso o worker grava o .txt temporário com o nome original.
    sem_ref = texto_analitica("01/2024", [("12345 6", "HNOME")]).replace(b"REFER\xc3\x8aNCIA: 01/2024", b"")
    rodar(enviar([BORDERO_JAN, ("Faturas-Saneago-Analítica/2024/hidrometros_01_2024.pdf", sem_ref)]))
    assert _hidrometros()[("123456", "01/2024")] == "HNOME"
