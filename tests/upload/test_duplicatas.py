"""Anti-duplicata (chave NUM_FATURA|MES_ANO_REF|CONTA_DV) visto pelo HTTP."""

import pytest

import utilitarios as u

pytestmark = pytest.mark.dados_reais


def _unicas(url, empresa):
    linhas = u.todas_linhas(url, empresa)
    return len(linhas), len({u.chave_fatura(l) for l in linhas})


def test_mesmo_arquivo_em_lotes_diferentes(servidor_limpo, fatura):
    nome, conteudo = fatura("SAAE_CORUMBA")
    s1, _ = u.rodar_job(servidor_limpo.url, [(nome, conteudo)])
    s2, _ = u.rodar_job(servidor_limpo.url, [(nome, conteudo)])
    r1 = s1["resultado"]["empresas"]["SAAE_CORUMBA"]
    r2 = s2["resultado"]["empresas"]["SAAE_CORUMBA"]
    assert r1["adicionadas"] >= 1 and r1["duplicadas"] == 0
    assert r2["adicionadas"] == 0
    assert r2["duplicadas"] == r1["adicionadas"]
    total, unicas = _unicas(servidor_limpo.url, "SAAE_CORUMBA")
    assert total == unicas == r1["adicionadas"]


def test_mesmo_conteudo_com_outro_nome_e_duplicata(servidor_limpo, fatura):
    nome, conteudo = fatura("SAAE_MINEIROS")
    u.rodar_job(servidor_limpo.url, [(nome, conteudo)])
    s2, _ = u.rodar_job(servidor_limpo.url, [("AGUA - SAAE MINEIROS/copia renomeada.pdf", conteudo)])
    assert s2["resultado"]["empresas"]["SAAE_MINEIROS"]["adicionadas"] == 0
    assert u.total_dados(servidor_limpo.url, "SAAE_MINEIROS") == 1


def test_saneago_reenviado_e_duplicata(servidor_limpo, fatura):
    """Lote de 145 linhas reenviado: nada novo."""
    nome, conteudo = fatura("SANEAGO")
    s1, _ = u.rodar_job(servidor_limpo.url, [(nome, conteudo)])
    s2, _ = u.rodar_job(servidor_limpo.url, [(nome, conteudo)])
    assert s2["resultado"]["empresas"]["SANEAGO"]["adicionadas"] == 0
    assert u.total_dados(servidor_limpo.url, "SANEAGO") == s1["resultado"]["empresas"]["SANEAGO"]["adicionadas"]


@pytest.mark.parametrize("csv_ja_existe", [False, True], ids=["csv_novo", "csv_existente"])
def test_mesmo_arquivo_duas_vezes_no_mesmo_lote(servidor_limpo, fatura, csv_ja_existe):
    if csv_ja_existe:
        u.rodar_job(servidor_limpo.url, [fatura("SAAE_MINEIROS")])
    nome, conteudo = fatura("SAAE_CORUMBA")
    lote = [(nome, conteudo), (nome.replace(".pdf", " (1).pdf"), conteudo)]
    snap, _ = u.rodar_job(servidor_limpo.url, lote)
    total, unicas = _unicas(servidor_limpo.url, "SAAE_CORUMBA")
    assert total == unicas
    resumo = snap["resultado"]["empresas"]["SAAE_CORUMBA"]
    assert resumo["duplicadas"] >= 1


@pytest.mark.lento
def test_pasta_com_faturas_repetidas_nao_gera_duplicata(servidor_limpo):
    pasta = u.pasta_distribuidora("DEMAE CALDAS")
    pdfs = [p for p in u.listar_pdfs(pasta) if "2021" in str(p)]
    lote = [(u.nome_webkit(p, pasta), u.ler_bytes(p)) for p in pdfs]
    u.rodar_job(servidor_limpo.url, lote)
    total, unicas = _unicas(servidor_limpo.url, "DEMAE")
    assert total == unicas
