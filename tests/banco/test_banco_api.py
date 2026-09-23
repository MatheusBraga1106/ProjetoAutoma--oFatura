"""Contrato HTTP sobre o banco (TestClient, sem lifespan: o worker é chamado
explicitamente)."""

import io
import json

import pytest
from apoio_banco import texto_demae, texto_saneago
from fastapi.testclient import TestClient

import worker

CONTAS = [("12345 6", "FORUM TESTE", "RUA A 1", 10, "100,00", "50,00", "5,00"),
          ("23456 7", "FORUM DOIS", "RUA B 2", 20, "200,00", "80,00", "9,99")]


@pytest.fixture
def cliente(ambiente):
    import api

    return TestClient(api.app)


def _postar(cliente, arquivos):
    partes = [("arquivos", (nome, io.BytesIO(c), "application/pdf")) for nome, c in arquivos]
    r = cliente.post("/pipeline/jobs", files=partes)
    assert r.status_code == 200, r.text
    return r.json()


def _sse(cliente, job_id):
    eventos, tipo = [], None
    with cliente.stream("GET", f"/pipeline/jobs/{job_id}/eventos") as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        for linha in r.iter_lines():
            if linha.startswith("event: "):
                tipo = linha[7:]
            elif linha.startswith("data: "):
                eventos.append((tipo, json.loads(linha[6:])))
    return eventos


def _carregar(cliente):
    saneago = texto_saneago("555001", "01/2024", CONTAS)
    demae = texto_demae("FATURA=1;MES=01/2024;CONTA=900-1;TOTAL=50.00",
                        "FATURA=2;MES=02/2024;CONTA=900-1;AGUA=10.00;OUTRAS=0;TOTAL=99.00")  # 2ª não fecha -> suspeita
    corpo = _postar(cliente, [("dados_entrada/Faturas-Saneago-Borderô/2024/jan.pdf", saneago),
                              ("dados_entrada/AGUA - DEMAE/2024/d.pdf", demae),
                              ("dados_entrada/AGUA - DEMAE/2024/d.pdf", demae),
                              ("nao_e_pdf.txt", b"x")])
    assert corpo["total_arquivos"] == 3
    assert worker.processar_proximo() == corpo["job_id"]
    return corpo["job_id"]


def test_fluxo_completo_do_job(cliente):
    assert cliente.post("/pipeline/jobs", files=[("arquivos", ("a.txt", b"x", "text/plain"))]).status_code == 400
    job_id = _carregar(cliente)

    snap = cliente.get(f"/pipeline/jobs/{job_id}").json()
    assert snap["status"] == "concluido" and snap["total_arquivos"] == 3
    empresas = snap["resultado"]["empresas"]
    assert empresas["SANEAGO"]["adicionadas"] == 2
    assert empresas["DEMAE"]["adicionadas"] == 2 and empresas["DEMAE"]["duplicadas"] == 2
    assert empresas["DEMAE"]["suspeitas"] == 1
    assert {"adicionadas", "duplicadas", "suspeitas", "descartadas", "contas_json_encontrado",
            "linhas_sem_correspondencia"} <= set(empresas["DEMAE"])

    eventos = _sse(cliente, job_id)
    assert [t for t, _ in eventos] == ["progresso"] * 3 + ["concluido"]
    p = eventos[0][1]
    assert {"arquivo", "indice", "total", "status", "empresa"} <= set(p)
    assert p["arquivo"] == "dados_entrada/Faturas-Saneago-Borderô/2024/jan.pdf"
    assert eventos[-1][1] == snap["resultado"]

    csv = cliente.get(f"/pipeline/jobs/{job_id}/csv/SANEAGO")
    assert csv.status_code == 200 and csv.headers["content-type"].startswith("text/csv")
    assert 'filename="banco_dados_saneago.csv"' in csv.headers["content-disposition"]
    texto = csv.content.decode("utf-8-sig").splitlines()
    assert texto[0].startswith("CONCESSIONARIA;NUM_FATURA;MES_ANO_REF") and len(texto) == 3
    assert cliente.get("/pipeline/jobs/naoexiste").status_code == 404
    assert cliente.get("/pipeline/jobs/naoexiste/eventos").status_code == 404
    assert cliente.get(f"/pipeline/jobs/{job_id}/csv/IPAMERI").status_code == 404
    assert cliente.get(f"/pipeline/jobs/{job_id}/csv/..%2Fx").status_code == 404


def test_dados_paginacao_busca_e_somente_suspeitas(cliente):
    _carregar(cliente)
    empresas = cliente.get("/dados/empresas").json()["empresas"]
    assert [(e["empresa"], e["linhas"]) for e in empresas] == [("SANEAGO", 2), ("DEMAE", 2)]

    r = cliente.get("/dados/DEMAE", params={"tamanho_pagina": 1}).json()
    assert r["total"] == 2 and len(r["linhas"]) == 1
    assert r["linhas"][0]["MES_ANO_REF"] == "02/2024"  # mais recente primeiro
    r2 = cliente.get("/dados/demae", params={"tamanho_pagina": 1, "pagina": 2}).json()
    assert r2["linhas"][0]["MES_ANO_REF"] == "01/2024"

    assert cliente.get("/dados/SANEAGO", params={"busca": "forum dois"}).json()["total"] == 1
    assert cliente.get("/dados/SANEAGO", params={"busca": "%"}).json()["total"] == 0  # curinga escapado

    sus = cliente.get("/dados/DEMAE", params={"somente_suspeitas": "1"}).json()
    assert sus["total"] == 1 and all(l["SUSPEITO"] for l in sus["linhas"])
    assert cliente.get("/dados/DEMAE", params={"somente_suspeitas": "true", "busca": "cliente"}).json()["total"] == 1
    assert cliente.get("/dados/DEMAE", params={"somente_suspeitas": "0"}).json()["total"] == 2
    assert cliente.get("/dados/SANEAGO", params={"somente_suspeitas": "1"}).json()["total"] == 0

    assert cliente.get("/dados/IPAMERI").status_code == 404
    assert cliente.get("/dados/XPTO").status_code == 404
    csv = cliente.get("/dados/DEMAE/csv")
    assert csv.status_code == 200 and len(csv.content.decode("utf-8-sig").splitlines()) == 3


def test_dashboard(cliente):
    _carregar(cliente)
    d = cliente.get("/dashboard/resumo").json()
    assert d["kpis"]["total_faturas"] == 4
    assert d["kpis"]["valor_total"] == round(155 + 289.99 + 50 + 99, 2)
    assert d["kpis"]["total_suspeitas"] == 1
    assert [e["empresa"] for e in d["por_empresa"]] == ["SANEAGO", "DEMAE"]
    so_demae = cliente.get("/dashboard/resumo", params={"empresa": "DEMAE"}).json()
    assert so_demae["kpis"] == d["kpis"]
    assert {t["empresa"] for t in so_demae["top_valor"]} == {"DEMAE"}
    assert [m["mes_ano"] for m in so_demae["serie_mensal"]] == ["01/2024", "02/2024"]
    assert cliente.get("/dashboard/resumo", params={"empresa": "XPTO"}).status_code == 404


def test_erros_status_health_manifest(cliente):
    _carregar(cliente)
    novo = cliente.post("/erros", json={"mensagem": " valor errado ", "concessionaria": "DEMAE",
                                        "num_fatura": "1", "conta_dv": "900-1", "mes_ano_ref": "01/2024"}).json()
    assert novo["mensagem"] == "valor errado" and novo["status"] == "aberto" and novo["fatura_id"]
    assert cliente.post("/erros", json={"mensagem": "  "}).status_code == 400
    assert cliente.patch(f"/erros/{novo['id']}", json={"status": "resolvido"}).json()["status"] == "resolvido"
    assert cliente.patch(f"/erros/{novo['id']}", json={"status": "xx"}).status_code == 400
    assert cliente.patch("/erros/9999", json={"status": "aberto"}).status_code == 404
    assert len(cliente.get("/erros", params={"status": "resolvido"}).json()["erros"]) == 1
    assert cliente.get("/erros", params={"status": "zzz"}).status_code == 400

    st = cliente.get("/pipeline/status-sistema").json()
    assert st["contas_json_encontrado"] is True and st["banco"] == "sqlite" and st["armazenamento"] == "local"
    assert cliente.get("/health").json() == {"status": "ok"}
    assert cliente.get("/manifest").json()["id"] == "faturas-agua"


def test_worker_embutido_processa_via_background_task(ambiente, monkeypatch):
    import api

    monkeypatch.setenv("WORKER_EMBUTIDO", "1")
    c = TestClient(api.app)  # sem "with": sem lifespan, só a background task
    corpo = _postar(c, [("demae/x.pdf", texto_demae("FATURA=1;MES=01/2024;CONTA=900-1;TOTAL=5.00"))])
    assert c.get(f"/pipeline/jobs/{corpo['job_id']}").json()["status"] == "concluido"


def test_upload_acima_do_limite_recusado_sem_criar_job(cliente, monkeypatch):
    import api
    from banco.modelos import Job
    from banco.sessao import sessao
    from sqlalchemy import func, select

    monkeypatch.setattr(api, "UPLOAD_MAX_BYTES", 1024)
    partes = [("arquivos", ("AGUA - DEMAE/grande.pdf", io.BytesIO(b"%PDF" + b"0" * 4096), "application/pdf"))]
    r = cliente.post("/pipeline/jobs", files=partes)
    assert r.status_code == 413
    assert "limite" in r.json()["detail"]
    with sessao() as s:
        assert s.scalar(select(func.count()).select_from(Job)) == 0


def test_sse_manda_id_e_retoma_do_last_event_id(cliente):
    job_id = _carregar(cliente)
    ids = []
    with cliente.stream("GET", f"/pipeline/jobs/{job_id}/eventos") as r:
        for linha in r.iter_lines():
            if linha.startswith("id: "):
                ids.append(int(linha[4:]))
    assert len(ids) == 4 and ids == sorted(ids)

    # Reconexão depois do 2º evento: só chegam os que faltavam, sem repetir.
    tipos = []
    with cliente.stream("GET", f"/pipeline/jobs/{job_id}/eventos",
                        headers={"Last-Event-ID": str(ids[1])}) as r:
        for linha in r.iter_lines():
            if linha.startswith("event: "):
                tipos.append(linha[7:])
    assert tipos == ["progresso", "concluido"]


def test_envio_com_mais_de_1000_arquivos(cliente):
    # Limite padrão do Starlette é 1000; o acervo real tem 1.117 PDFs.
    partes = [("arquivos", (f"AGUA - DEMAE/{i}.pdf", io.BytesIO(b"%PDF-" + str(i).encode()), "application/pdf"))
              for i in range(1100)]
    r = cliente.post("/pipeline/jobs", files=partes)
    assert r.status_code == 200, r.text
    assert r.json()["total_arquivos"] == 1100


def test_autenticacao_basica_quando_senha_definida(cliente, monkeypatch):
    import api

    assert cliente.get("/dados/empresas").status_code == 200  # sem APP_SENHA: aberto (dev)

    monkeypatch.setattr(api, "APP_USUARIO", "faturas")
    monkeypatch.setattr(api, "APP_SENHA", "s3nha-teste")
    r = cliente.get("/dados/empresas")
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Basic")
    assert cliente.get("/", auth=("faturas", "errada")).status_code == 401
    assert cliente.get("/dados/empresas", auth=("outro", "s3nha-teste")).status_code == 401
    assert cliente.get("/dados/empresas", auth=("faturas", "s3nha-teste")).status_code == 200
    assert cliente.get("/static/app.js", auth=("faturas", "s3nha-teste")).status_code == 200
    assert cliente.get("/health").status_code == 200  # HEALTHCHECK do Docker sem credencial
    assert cliente.get("/dados/empresas", headers={"Authorization": "Basic %%%"}).status_code == 401
