"""Jobs concorrentes, SSE (desconexão/reconexão) e reinício do servidor."""

import os
import threading
import time

import httpx
import pytest

import utilitarios as u

pytestmark = pytest.mark.dados_reais


def _disparar_simultaneos(url, lotes):
    """Envia os lotes ao mesmo tempo (barreira) e devolve os job_ids."""
    ids = [None] * len(lotes)
    barreira = threading.Barrier(len(lotes))

    def enviar(i):
        barreira.wait()
        r = u.criar_job(url, lotes[i])
        assert r.status_code == 200, r.text
        ids[i] = r.json()["job_id"]

    threads = [threading.Thread(target=enviar, args=(i,)) for i in range(len(lotes))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return ids


def _pdfs_mineiros(n, pular=0):
    pasta = u.pasta_distribuidora("MINEIROS")
    return [(u.nome_webkit(p, pasta), u.ler_bytes(p)) for p in u.listar_pdfs(pasta)[pular:pular + n]]


def test_jobs_concorrentes_todos_terminam_com_sse_completo(servidor):
    lotes = [_pdfs_mineiros(2, pular=10 + 2 * i) for i in range(3)]
    ids = _disparar_simultaneos(servidor.url, lotes)
    assert len(set(ids)) == 3
    resultados = [None] * 3

    def ler(i):
        resultados[i] = u.ler_eventos(servidor.url, ids[i], timeout=300)

    threads = [threading.Thread(target=ler, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for lote, eventos, job_id in zip(lotes, resultados, ids):
        u.validar_sequencia_sse(eventos, [n for n, _ in lote])  # sem vazar evento de outro job
        assert u.aguardar_job(servidor.url, job_id)["status"] == "concluido"


def test_jobs_concorrentes_nao_perdem_nem_duplicam_linhas(servidor_limpo):
    lotes = [[a] for a in _pdfs_mineiros(6, pular=2)]
    ids = _disparar_simultaneos(servidor_limpo.url, lotes)
    snaps = [u.aguardar_job(servidor_limpo.url, j) for j in ids]
    adicionadas = sum(s["resultado"]["empresas"]["SAAE_MINEIROS"]["adicionadas"] for s in snaps)
    linhas = u.todas_linhas(servidor_limpo.url, "SAAE_MINEIROS")
    assert len(linhas) == adicionadas == 6
    assert len({u.chave_fatura(l) for l in linhas}) == 6


def test_mesmo_arquivo_em_jobs_concorrentes(servidor_limpo, fatura):
    a = fatura("SAAE_MINEIROS")
    ids = _disparar_simultaneos(servidor_limpo.url, [[a]] * 4)
    snaps = [u.aguardar_job(servidor_limpo.url, j) for j in ids]
    adicionadas = sum(s["resultado"]["empresas"]["SAAE_MINEIROS"]["adicionadas"] for s in snaps)
    assert u.total_dados(servidor_limpo.url, "SAAE_MINEIROS") == 1
    assert adicionadas == 1


def test_servidor_responde_durante_job(servidor, fatura):
    lote = _pdfs_mineiros(8, pular=20)
    job_id = u.criar_job(servidor.url, lote).json()["job_id"]
    latencias = []
    while u.snapshot(servidor.url, job_id).json()["status"] not in ("concluido", "erro"):
        t = time.time()
        assert httpx.get(servidor.url + "/health", timeout=5).status_code == 200
        latencias.append(time.time() - t)
        time.sleep(0.2)
    assert max(latencias, default=0) < 2.0, latencias


def test_cliente_desconecta_e_reconecta_sse(servidor):
    lote = _pdfs_mineiros(6, pular=26)
    nomes = [n for n, _ in lote]
    job_id = u.criar_job(servidor.url, lote).json()["job_id"]
    parcial = u.ler_eventos(servidor.url, job_id, parar_apos=2)  # cliente cai
    assert [e.tipo for e in parcial] == ["progresso", "progresso"]
    # o job continua rodando sem ninguém ouvindo
    final = u.aguardar_job(servidor.url, job_id)
    assert final["status"] == "concluido"
    # reconexão recebe o histórico completo (replay desde o início)
    eventos = u.ler_eventos(servidor.url, job_id)
    u.validar_sequencia_sse(eventos, nomes)


def test_sse_retoma_a_partir_do_last_event_id(servidor):
    lote = _pdfs_mineiros(3, pular=32)
    job_id = u.criar_job(servidor.url, lote).json()["job_id"]
    parcial = u.ler_eventos(servidor.url, job_id, parar_apos=1)
    assert parcial[0].id is not None
    resto = u.ler_eventos(servidor.url, job_id, cabecalhos={"Last-Event-ID": parcial[0].id})
    assert resto[0].dados.get("indice") == 2


@pytest.mark.lento
def test_sse_manda_algo_a_cada_15s_durante_ocr_longo(servidor):
    pasta = u.pasta_distribuidora("DEMAE PANAMA")
    grande = max(u.listar_pdfs(pasta), key=lambda p: os.path.getsize(u.caminho_longo(p)))
    lote = [(u.nome_webkit(grande, pasta), u.ler_bytes(grande))]
    job_id = u.criar_job(servidor.url, lote).json()["job_id"]
    ultimo = time.time()
    maior_silencio = 0.0
    with httpx.stream("GET", f"{servidor.url}/pipeline/jobs/{job_id}/eventos", timeout=600) as r:
        for _ in r.iter_raw():
            agora = time.time()
            maior_silencio = max(maior_silencio, agora - ultimo)
            ultimo = agora
    print(f"\nMEDIDA maior silêncio no SSE: {maior_silencio:.1f}s")
    assert maior_silencio < 15


@pytest.mark.lento
def test_reinicio_do_servidor_no_meio_do_job(tmp_path):
    s = u.novo_servidor(tmp_path / "srv", 8029).iniciar()
    try:
        lote = _pdfs_mineiros(20, pular=0)
        job_id = u.criar_job(s.url, lote).json()["job_id"]
        u.ler_eventos(s.url, job_id, parar_apos=2)
        s.parar()  # queda abrupta (kill)
        restos = [n for n in os.listdir(s.pasta_temp) if n.startswith("pipeline_")]
        s.iniciar()
        # contrato atual: job em memória some; o novo design deve devolver o job
        r = u.snapshot(s.url, job_id)
        registro = {"status_http_apos_restart": r.status_code, "pdfs_temporarios_orfaos": restos}
        print("\nMEDIDA restart:", registro)
        assert r.status_code in (200, 404)
        if r.status_code == 200:
            assert r.json()["status"] in u.STATUS_JOB
        # nada gravado pela metade: ou o job todo ou nada (hoje: nada)
        assert u.total_dados(s.url, "SAAE_MINEIROS") in (0, 20)
    finally:
        s.parar()


@pytest.mark.lento
def test_reinicio_nao_perde_job_nem_deixa_temporarios(tmp_path):
    s = u.novo_servidor(tmp_path / "srv", 8028).iniciar()
    try:
        lote = _pdfs_mineiros(20, pular=0)
        job_id = u.criar_job(s.url, lote).json()["job_id"]
        u.ler_eventos(s.url, job_id, parar_apos=1)
        s.parar()
        s.iniciar()
        assert u.snapshot(s.url, job_id).status_code == 200
        time.sleep(2)
        assert not [n for n in os.listdir(s.pasta_temp) if n.startswith("pipeline_")]
    finally:
        s.parar()
