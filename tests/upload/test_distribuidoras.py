"""Um PDF real de cada distribuidora, sozinho e em lote misto."""

import pytest

import utilitarios as u
from conftest import AMOSTRAS

pytestmark = pytest.mark.dados_reais

PERSISTIDAS = [e for e in AMOSTRAS if e != "SANEAGO_ANALITICA"]


@pytest.mark.parametrize("empresa", sorted(AMOSTRAS))
def test_arquivo_unico_por_distribuidora(servidor_limpo, fatura, empresa):
    nome, conteudo = fatura(empresa)
    snap, eventos = u.rodar_job(servidor_limpo.url, [(nome, conteudo)])
    assert snap["status"] == "concluido"
    u.validar_sequencia_sse(eventos, [nome])
    u.validar_resultado(snap["resultado"], [nome])
    arquivo = snap["resultado"]["arquivos"][0]
    assert arquivo["status"] == "ok", arquivo
    assert arquivo["empresa"] == empresa
    if empresa in PERSISTIDAS:
        resumo = snap["resultado"]["empresas"][empresa]
        assert resumo["adicionadas"] + resumo["descartadas"] == arquivo["linhas"]
        assert u.total_dados(servidor_limpo.url, empresa) == resumo["adicionadas"]


def test_lote_misto_todas_distribuidoras(servidor_limpo, fatura):
    arquivos = [fatura(e) for e in sorted(AMOSTRAS)]
    nomes = [n for n, _ in arquivos]
    snap, eventos = u.rodar_job(servidor_limpo.url, arquivos)
    u.validar_sequencia_sse(eventos, nomes)
    u.validar_resultado(snap["resultado"], nomes)
    por_nome = {a["arquivo_origem"]: a for a in snap["resultado"]["arquivos"]}
    for empresa, (nome, _) in zip(sorted(AMOSTRAS), arquivos):
        assert por_nome[nome]["status"] == "ok"
        assert por_nome[nome]["empresa"] == empresa
    assert set(snap["resultado"]["empresas"]) == set(PERSISTIDAS)
    for empresa in PERSISTIDAS:
        assert u.total_dados(servidor_limpo.url, empresa) == snap["resultado"]["empresas"][empresa]["adicionadas"]


def test_saneago_com_analitica_no_mesmo_lote_preenche_hidrometro(servidor_limpo, fatura):
    snap, _ = u.rodar_job(servidor_limpo.url, [fatura("SANEAGO"), fatura("SANEAGO_ANALITICA")])
    assert snap["status"] == "concluido"
    linhas = u.todas_linhas(servidor_limpo.url, "SANEAGO")
    com_hidrometro = [l for l in linhas if l.get("NUM_HIDROMETRO") not in (None, "", "nan")]
    assert len(com_hidrometro) >= len(linhas) * 0.5, (len(com_hidrometro), len(linhas))


def test_analitica_em_lote_separado_enriquece_saneago_posterior(servidor_limpo, fatura):
    snap1, _ = u.rodar_job(servidor_limpo.url, [fatura("SANEAGO_ANALITICA")])
    assert snap1["resultado"]["arquivos"][0]["status"] == "ok"
    u.rodar_job(servidor_limpo.url, [fatura("SANEAGO")])
    linhas = u.todas_linhas(servidor_limpo.url, "SANEAGO")
    com_hidrometro = [l for l in linhas if l.get("NUM_HIDROMETRO") not in (None, "", "nan")]
    assert len(com_hidrometro) >= len(linhas) * 0.5


def test_distribuidora_sem_extrator_e_nao_identificada(servidor):
    """SANESC/CHESP têm padrão mas não extrator; SSSA/Leopoldo nem padrão."""
    casos = {
        "SANESC": u.pasta_distribuidora("SANESC"),
        "CHESP": u.pasta_distribuidora("CHESP"),
        None: u.pasta_distribuidora("SAO SIMAO"),
    }
    arquivos = []
    for pasta in casos.values():
        p = u.listar_pdfs(pasta)[0]
        arquivos.append((u.nome_webkit(p, pasta), u.ler_bytes(p)))
    snap, eventos = u.rodar_job(servidor.url, arquivos)
    u.validar_sequencia_sse(eventos, [n for n, _ in arquivos])
    erros = [a["erro"] for a in snap["resultado"]["arquivos"]]
    assert all(a["status"] == "erro" for a in snap["resultado"]["arquivos"])
    assert "SANESC" in erros[0] and "implementado" in erros[0]
    assert "CHESP" in erros[1] and "implementado" in erros[1]
    assert "identificar" in erros[2]
