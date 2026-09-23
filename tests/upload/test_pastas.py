"""Upload de pasta (simulando <input webkitdirectory>: filename do multipart
= caminho relativo) vs. upload de arquivo solto (só o basename)."""

import json
import time

import pytest

import utilitarios as u

pytestmark = pytest.mark.dados_reais


def _lote_da_pasta(pasta, limite=None, solto=False):
    pdfs = u.listar_pdfs(pasta)[:limite]
    return [(p.name if solto else u.nome_webkit(p, pasta), u.ler_bytes(p)) for p in pdfs]


def _nao_identificados(snap):
    return [a for a in snap["resultado"]["arquivos"] if a["status"] == "erro" and "identificar" in a["erro"]]


def test_filename_com_barra_preserva_caminho_relativo(servidor, fatura):
    """O servidor precisa receber o caminho relativo intacto (com "/") —
    é o que permite identificar a distribuidora pelo nome da pasta."""
    _, conteudo = fatura("SAE")
    nome = "AGUA - SAE CATALÃO/2021/sub/pasta/11723-4.pdf"
    snap, eventos = u.rodar_job(servidor.url, [(nome, conteudo)])
    assert eventos[0].dados["arquivo"] == nome
    assert snap["resultado"]["arquivos"][0]["arquivo_origem"] == nome
    assert snap["resultado"]["arquivos"][0]["empresa"] == "SAE"


def test_pasta_aninhada_saneago_bordero_um_ano(servidor_limpo):
    """Faturas-Saneago-Borderô/2021: vários arquivos sem "SANEAGO" no nome."""
    pasta = u.SANEAGO_BORDERO
    lote = [a for a in _lote_da_pasta(pasta) if a[0].split("/")[1] == "2021"]
    assert lote
    snap, eventos = u.rodar_job(servidor_limpo.url, lote)
    u.validar_sequencia_sse(eventos, [n for n, _ in lote])
    assert _nao_identificados(snap) == []
    assert all(a.get("empresa") in (None, "SANEAGO") for a in snap["resultado"]["arquivos"])


def test_selecionar_pasta_raiz_dados_entrada(servidor):
    """Selecionar dados_entrada/ inteira: o caminho começa pela raiz e
    atravessa Faturas-Outras-Distribuídoras/<DISTRIBUIDORA>/<ano>/."""
    pasta = u.pasta_distribuidora("SAE CATALAO")
    pdfs = u.listar_pdfs(pasta)[:3]
    lote = [(u.nome_webkit(p, u.DADOS_ENTRADA), u.ler_bytes(p)) for p in pdfs]
    assert all(n.startswith(u.DADOS_ENTRADA.name + "/Faturas-Outras-") for n, _ in lote)
    snap, _ = u.rodar_job(servidor.url, lote)
    assert _nao_identificados(snap) == []


def test_mesmos_arquivos_soltos_da_analitica_nao_sao_identificados(servidor):
    """Problema conhecido, medido: na pasta Faturas-Saneago-Analítica só 7 de
    65 PDFs têm a palavra-chave no nome. Solto, o resto falha."""
    pasta = u.SANEAGO_ANALITICA
    lote_pasta = _lote_da_pasta(pasta, limite=12)
    lote_solto = _lote_da_pasta(pasta, limite=12, solto=True)
    # o nome solto sem distribuidora nem é processado (falha já na identificação)
    snap_solto, _ = u.rodar_job(servidor.url, [a for a in lote_solto if "saneago" not in a[0].lower()])
    assert len(_nao_identificados(snap_solto)) == len(snap_solto["resultado"]["arquivos"]) > 0
    # os mesmos bytes, com o caminho da pasta, são identificados
    nomes_sem_chave = {a[0] for a in lote_solto if "saneago" not in a[0].lower()}
    lote_pasta = [a for a in lote_pasta if a[0].rsplit("/", 1)[-1] in nomes_sem_chave]
    snap_pasta, _ = u.rodar_job(servidor.url, lote_pasta)
    assert _nao_identificados(snap_pasta) == []


@pytest.mark.xfail(strict=False, reason="Problema conhecido: upload de arquivo solto sem a palavra-chave no "
                   "nome não identifica a distribuidora (pipeline.identificar_distribuidora só olha o nome). "
                   "Medido no acervo: 124 de 725 PDFs de distribuidoras implementadas (17%) — 58/65 da "
                   "SANEAGO analítica, 30/200 DEMAE Caldas Novas, 11/68 SAE, 7/68 Abadiânia, 6/68 Corumbá, "
                   "6/68 SANEAGO borderô, 5/52 DEMAE Panamá, 1/53 Buriti Alegre.")
def test_arquivo_solto_deveria_ser_identificado(servidor, fatura):
    _, conteudo = fatura("SAE")
    snap, _ = u.rodar_job(servidor.url, [("11723-4.pdf", conteudo)])
    assert snap["resultado"]["arquivos"][0]["status"] == "ok"


@pytest.mark.xfail(strict=False, reason="Selecionar só a subpasta do ano (ex.: 'AGUA - SAE CATALÃO/2021') "
                   "perde o nome da distribuidora no webkitRelativePath — mesmo efeito do arquivo solto.")
def test_selecionar_so_pasta_do_ano(servidor):
    pasta = u.pasta_distribuidora("SAE CATALAO") / "2021"
    lote = _lote_da_pasta(pasta, limite=4)
    snap, _ = u.rodar_job(servidor.url, lote)
    assert _nao_identificados(snap) == []


def _medir(url, lote):
    t0 = time.time()
    r = u.criar_job(url, lote)
    t_upload = time.time() - t0
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    eventos = u.ler_eventos(url, job_id, timeout=3600)
    t_total = time.time() - t0
    snap = u.aguardar_job(url, job_id)
    tempos = [b.recebido_em - a.recebido_em for a, b in zip(eventos, eventos[1:]) if b.tipo == "progresso"]
    return snap, eventos, {
        "arquivos": len(lote),
        "mb": round(sum(len(c) for _, c in lote) / 1e6, 1),
        "upload_s": round(t_upload, 1),
        "total_s": round(t_total, 1),
        "por_arquivo_s": round(t_total / len(lote), 2),
        "maior_intervalo_entre_eventos_s": round(max(tempos, default=0), 1),
    }


@pytest.mark.lento
def test_distribuidora_inteira_saae_abadiania(servidor_limpo):
    pasta = u.pasta_distribuidora("ABADIANIA")
    lote = _lote_da_pasta(pasta)
    assert len(lote) == 68
    snap, eventos, medida = _medir(servidor_limpo.url, lote)
    print("\nMEDIDA abadiania:", json.dumps(medida))
    u.validar_sequencia_sse(eventos, [n for n, _ in lote])
    u.validar_resultado(snap["resultado"], [n for n, _ in lote])
    assert _nao_identificados(snap) == []
    resumo = snap["resultado"]["empresas"]["SAAE_ABADIANIA"]
    assert u.total_dados(servidor_limpo.url, "SAAE_ABADIANIA") == resumo["adicionadas"]


@pytest.mark.lento
def test_lote_grande_misto(servidor_limpo):
    """~250 PDFs de 6 distribuidoras (incluindo SANEAGO+analítica e faturas
    que caem no OCR), como se o usuário selecionasse várias pastas."""
    lote = []
    for pasta, limite in [
        (u.SANEAGO_BORDERO, 24), (u.SANEAGO_ANALITICA, 24),
        (u.pasta_distribuidora("DEMAE CALDAS"), 80), (u.pasta_distribuidora("SAE CATALAO"), 40),
        (u.pasta_distribuidora("CORUMBA"), 40), (u.pasta_distribuidora("MINEIROS"), 39),
        (u.pasta_distribuidora("IPAMERI"), 10),
    ]:
        lote += _lote_da_pasta(pasta, limite=limite)
    assert len(lote) >= 200
    snap, eventos, medida = _medir(servidor_limpo.url, lote)
    print("\nMEDIDA lote misto:", json.dumps(medida))
    nomes = [n for n, _ in lote]
    u.validar_sequencia_sse(eventos, nomes)
    u.validar_resultado(snap["resultado"], nomes)
    assert _nao_identificados(snap) == []
    for empresa, resumo in snap["resultado"]["empresas"].items():
        assert u.total_dados(servidor_limpo.url, empresa) == resumo["adicionadas"], empresa
