"""Contrato HTTP do fluxo de upload: criação de job, snapshot, SSE, CSV,
validação de entrada. Referência: api.py no commit 67cebcf."""

import csv
import io

import httpx
import pytest

import utilitarios as u

pytestmark = pytest.mark.dados_reais


def test_criar_job_devolve_id_e_total(servidor, fatura):
    nome, conteudo = fatura("SAAE_MINEIROS")
    r = u.criar_job(servidor.url, [(nome, conteudo)])
    assert r.status_code == 200
    corpo = r.json()
    assert isinstance(corpo["job_id"], str) and corpo["job_id"]
    assert corpo["total_arquivos"] == 1
    u.aguardar_job(servidor.url, corpo["job_id"])


def test_snapshot_do_job(servidor, fatura):
    nome, conteudo = fatura("SAAE_MINEIROS")
    job_id = u.criar_job(servidor.url, [(nome, conteudo)]).json()["job_id"]
    r = u.snapshot(servidor.url, job_id)
    assert r.status_code == 200
    corpo = r.json()
    assert set(corpo) >= {"job_id", "status", "total_arquivos", "resultado"}
    assert corpo["job_id"] == job_id
    assert corpo["status"] in u.STATUS_JOB
    final = u.aguardar_job(servidor.url, job_id)
    assert final["status"] == "concluido"
    u.validar_resultado(final["resultado"], [nome])
    assert final["resultado"]["arquivos"][0]["status"] == "ok"
    assert final["resultado"]["arquivos"][0]["empresa"] == "SAAE_MINEIROS"


def test_sse_formato_e_ordem(servidor, fatura):
    arquivos = [fatura("SAAE_MINEIROS"), fatura("SAE"), ("AGUA - DESCONHECIDA/x.pdf", u.pdf_minimo())]
    nomes = [n for n, _ in arquivos]
    snap, eventos = u.rodar_job(servidor.url, arquivos)
    terminal = u.validar_sequencia_sse(eventos, nomes)
    assert eventos[-1].tipo == "concluido"
    # o evento "concluido" carrega o mesmo resultado do snapshot
    assert terminal == snap["resultado"]
    assert [e.dados["status"] for e in eventos[:-1]] == ["ok", "ok", "erro"]


def test_sse_depois_do_job_concluido_reenvia_historico(servidor, fatura):
    nome, conteudo = fatura("IPAMERI")
    snap, _ = u.rodar_job(servidor.url, [(nome, conteudo)])
    eventos = u.ler_eventos(servidor.url, snap["job_id"], timeout=30)
    u.validar_sequencia_sse(eventos, [nome])


def test_csv_do_job(servidor, fatura):
    nome, conteudo = fatura("SAAE_CORUMBA")
    snap, _ = u.rodar_job(servidor.url, [(nome, conteudo)])
    r = httpx.get(f"{servidor.url}/pipeline/jobs/{snap['job_id']}/csv/SAAE_CORUMBA", timeout=30)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    texto = r.content.decode("utf-8-sig")
    linhas = list(csv.DictReader(io.StringIO(texto), delimiter=";"))
    assert linhas, "CSV vazio"
    for coluna in ("CONCESSIONARIA", "NUM_FATURA", "MES_ANO_REF", "CONTA_DV", "VALOR_TOTAL", "ARQUIVO_ORIGEM"):
        assert coluna in linhas[0], coluna
    # empresa em minúsculas também é aceita
    r2 = httpx.get(f"{servidor.url}/pipeline/jobs/{snap['job_id']}/csv/saae_corumba", timeout=30)
    assert r2.status_code == 200


def test_arquivo_origem_e_pasta_origem_gravados(servidor_limpo, fatura):
    nome, conteudo = fatura("SAAE_MINEIROS")
    u.rodar_job(servidor_limpo.url, [(nome, conteudo)])
    linhas = u.todas_linhas(servidor_limpo.url, "SAAE_MINEIROS")
    assert len(linhas) == 1
    pasta, _, arquivo = nome.rpartition("/")
    assert linhas[0]["ARQUIVO_ORIGEM"] == arquivo
    assert linhas[0]["PASTA_ORIGEM"] == pasta


@pytest.mark.parametrize("caminho", [
    "/pipeline/jobs/nao-existe",
    "/pipeline/jobs/nao-existe/eventos",
    "/pipeline/jobs/nao-existe/csv/SAAE_MINEIROS",
])
def test_job_inexistente_404(servidor, caminho):
    assert httpx.get(servidor.url + caminho, timeout=10).status_code == 404


def test_csv_empresa_invalida_404(servidor, fatura):
    snap, _ = u.rodar_job(servidor.url, [fatura("SAAE_MINEIROS")])
    for empresa in ("NAO_EXISTE", "..%2F..%2Fcontas", "..%5Capi"):
        r = httpx.get(f"{servidor.url}/pipeline/jobs/{snap['job_id']}/csv/{empresa}", timeout=10)
        assert r.status_code == 404, empresa


def test_sem_campo_arquivos_400(servidor):
    # Era 422 (validação automática do FastAPI); desde que o formulário passou
    # a ser lido à mão (limite de arquivos acima de 1000) é 400 com mensagem.
    r = httpx.post(servidor.url + "/pipeline/jobs", timeout=10)
    assert r.status_code == 400 and r.json().get("detail")
    r = httpx.post(servidor.url + "/pipeline/jobs",
                   files=[("outro_campo", ("x SAAE MINEIROS.pdf", b"%PDF", "application/pdf"))], timeout=10)
    assert r.status_code == 400 and r.json().get("detail")


def test_so_arquivos_nao_pdf_400(servidor):
    r = u.criar_job(servidor.url, [("a.txt", b"x"), ("b.png", b"y"), ("c.pdf.exe", b"z")])
    assert r.status_code == 400
    assert "PDF" in r.json()["detail"]


def test_nao_pdf_misturado_e_ignorado(servidor, fatura):
    nome, conteudo = fatura("SAAE_MINEIROS")
    r = u.criar_job(servidor.url, [("notas.txt", b"x"), (nome, conteudo), ("foto.jpg", b"y")])
    assert r.status_code == 200
    assert r.json()["total_arquivos"] == 1
    final = u.aguardar_job(servidor.url, r.json()["job_id"])
    u.validar_resultado(final["resultado"], [nome])


def test_status_sistema(servidor):
    r = httpx.get(servidor.url + "/pipeline/status-sistema", timeout=10)
    assert r.status_code == 200
    corpo = r.json()
    assert {"contas_json_encontrado", "tesseract_resolvido", "pdftotext_resolvido",
            "distribuidoras_implementadas", "distribuidoras_nao_implementadas"} <= set(corpo)
    # a instância de teste nunca pode escrever na pasta de saída real
    assert str(servidor.pasta_saida) == corpo.get("pasta_saida", str(servidor.pasta_saida))
