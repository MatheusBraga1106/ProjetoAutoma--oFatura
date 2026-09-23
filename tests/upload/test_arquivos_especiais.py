"""Nomes estranhos, extensões, arquivos inválidos e PDF-imagem (OCR)."""

import os
import unicodedata

import pytest

import utilitarios as u

pytestmark = pytest.mark.dados_reais

NOMES_ESTRANHOS = [
    "AGUA - SAAE MINEIROS/2023 - INÍCIO (ESTÁ COMPLETO)/FATURA Nº 1 ª (1) [2] & ; ' % # espaço.pdf",
    "AGUA - SAAE MINEIROS/sub/" + "N" * 250 + ".pdf",  # caminho relativo > 260
    unicodedata.normalize("NFD", "ÁGUA - SAAE MINEIRÓS/fatura ção.pdf"),  # macOS manda NFD
    "AGUA - SAAE MINEIROS/../../../../etc/passwd.pdf",  # só rótulo; nunca vira caminho em disco
    "AGUA - SAAE MINEIROS/FATURA.PDF",  # extensão maiúscula
    "AGUA - SAAE MINEIROS/FATURA.Pdf",
]


@pytest.mark.parametrize("nome", NOMES_ESTRANHOS, ids=range(len(NOMES_ESTRANHOS)))
def test_nomes_com_acento_espaco_simbolos_e_longos(servidor, fatura, nome):
    _, conteudo = fatura("SAAE_MINEIROS")
    snap, eventos = u.rodar_job(servidor.url, [(nome, conteudo)])
    u.validar_sequencia_sse(eventos, [nome])
    a = snap["resultado"]["arquivos"][0]
    assert a["arquivo_origem"] == nome
    assert a["status"] == "ok", a
    assert a["empresa"] == "SAAE_MINEIROS"


def test_caminho_real_mais_longo_do_acervo(servidor):
    """Maior caminho real (> 260 caracteres absolutos no disco)."""
    pdfs = u.listar_pdfs(u.OUTRAS)
    maior = max(pdfs, key=lambda p: len(str(p)))
    assert len(str(maior)) > 260
    pasta = u.OUTRAS / os.path.relpath(str(maior), str(u.OUTRAS)).split(os.sep)[0]
    nome = u.nome_webkit(maior, pasta)
    snap, eventos = u.rodar_job(servidor.url, [(nome, u.ler_bytes(maior))])
    u.validar_sequencia_sse(eventos, [nome])
    a = snap["resultado"]["arquivos"][0]
    assert a["arquivo_origem"] == nome
    assert a["status"] == "ok" or "identificar" not in a["erro"]


def test_caminho_relativo_com_barra_invertida(servidor, fatura):
    """Relativo com '\\' é preservado. (Observado: um caminho ABSOLUTO estilo
    'C:\\...\\x.pdf' chega só como 'x.pdf' — python-multipart corta; não é
    o que o navegador manda, então não é testado como contrato.)"""
    _, conteudo = fatura("SAE")
    nome = "AGUA - SAE CATALÃO\\2021\\11723-4.pdf"
    snap, _ = u.rodar_job(servidor.url, [(nome, conteudo)])
    assert snap["resultado"]["arquivos"][0]["status"] == "ok"


INVALIDOS = {
    "vazio": b"",
    "corrompido": b"%PDF-1.4\n" + bytes(range(256)) * 20,
    "texto_com_extensao_pdf": "isto não é um PDF\n".encode() * 200,
    "html_com_extensao_pdf": b"<html><body>fatura</body></html>",
}


def test_arquivos_invalidos_viram_erro_por_arquivo_sem_derrubar_lote(servidor, fatura):
    bom_nome, bom = fatura("SAAE_MINEIROS")
    lote = [(f"AGUA - SAAE MINEIROS/{k}.pdf", v) for k, v in INVALIDOS.items()] + [(bom_nome, bom)]
    nomes = [n for n, _ in lote]
    snap, eventos = u.rodar_job(servidor.url, lote)
    assert snap["status"] == "concluido"
    u.validar_sequencia_sse(eventos, nomes)
    u.validar_resultado(snap["resultado"], nomes)
    status = [a["status"] for a in snap["resultado"]["arquivos"]]
    assert status == ["erro"] * len(INVALIDOS) + ["ok"]
    assert "SAAE_MINEIROS" in snap["resultado"]["empresas"]


def test_erro_de_pdf_invalido_nao_expoe_caminho_do_servidor(servidor):
    snap, eventos = u.rodar_job(servidor.url, [("AGUA - SAAE MINEIROS/vazio.pdf", b"")])
    detalhe = eventos[0].dados["detalhe"]
    assert "\\" not in detalhe and "/" not in detalhe and "pipeline_" not in detalhe, detalhe


def test_pdf_valido_sem_fatura(servidor):
    nome = "AGUA - SAAE MINEIROS/sem fatura.pdf"
    snap, _ = u.rodar_job(servidor.url, [(nome, u.pdf_minimo())])
    a = snap["resultado"]["arquivos"][0]
    assert a["status"] == "erro"
    assert a["erro"]


def test_pdf_imagem_passa_pelo_ocr(servidor_limpo, fatura):
    """Rasteriza uma fatura real (tira a camada de texto) e confere que o
    OCR (Tesseract) recupera dados suficientes pro extrator."""
    status = __import__("httpx").get(servidor_limpo.url + "/pipeline/status-sistema", timeout=10).json()
    if not status["tesseract_resolvido"]:
        pytest.skip("Tesseract não encontrado")
    nome, conteudo = fatura("SAAE_MINEIROS")
    imagem = u.pdf_imagem_de(conteudo, dpi=200)
    snap, _ = u.rodar_job(servidor_limpo.url, [(nome.replace(".pdf", " (imagem).pdf"), imagem)])
    a = snap["resultado"]["arquivos"][0]
    assert a["status"] == "ok", a
    assert u.total_dados(servidor_limpo.url, "SAAE_MINEIROS") >= 1


@pytest.mark.lento
def test_pdf_imagem_real_do_acervo(servidor):
    """DEMAE Panamá: 100% das faturas são imagem (OCR). Hoje o extrator não
    reconhece nenhuma — aqui só conferimos que o OCR roda e o job termina."""
    pasta = u.pasta_distribuidora("DEMAE PANAMA")
    p = u.listar_pdfs(pasta)[0]
    nome = u.nome_webkit(p, pasta)
    snap, eventos = u.rodar_job(servidor.url, [(nome, u.ler_bytes(p))])
    assert snap["status"] == "concluido"
    u.validar_sequencia_sse(eventos, [nome])
