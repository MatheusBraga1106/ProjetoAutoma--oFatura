"""Jobs persistidos: retomada após crash, lease, concorrência entre workers."""

import threading
from datetime import timedelta

import pytest
from apoio_banco import contar, enviar, faturas_ativas, texto_demae
from sqlalchemy import select, update

import ingestao
import worker
from banco.modelos import Fatura, Job, JobArquivo, JobEvento, agora
from banco.sessao import sessao


def _tres_arquivos():
    return [(f"demae/{i}.pdf", texto_demae(f"FATURA={i};MES=0{i}/2024;CONTA=900-1;TOTAL={i}0.00")) for i in (1, 2, 3)]


def _itens(job_id):
    with sessao() as s:
        return list(s.scalars(select(JobArquivo).where(JobArquivo.job_id == job_id).order_by(JobArquivo.indice)))


def _eventos(job_id):
    with sessao() as s:
        return [(e.tipo, e.dados) for e in s.scalars(select(JobEvento).where(JobEvento.job_id == job_id)
                                                     .order_by(JobEvento.id))]


def _envelhecer_heartbeat(job_id):
    with sessao(escrita=True) as s:
        s.execute(update(Job).where(Job.id == job_id).values(heartbeat_em=agora() - timedelta(hours=1)))


def test_job_retomado_apos_crash_no_meio(ambiente):
    job_id = enviar(_tres_arquivos())
    a = worker.novo_worker_id()
    assert worker.pegar_proximo_job(a) == job_id
    primeiro, segundo, _ = _itens(job_id)
    ingestao.processar_item(primeiro.id, a)       # 1º arquivo entrou
    ingestao.iniciar_item(segundo.id, a)          # começou o 2º e o processo "morreu" aqui

    b = worker.novo_worker_id()
    assert worker.pegar_proximo_job(b) is None     # heartbeat ainda fresco: ninguém rouba
    _envelhecer_heartbeat(job_id)
    assert worker.pegar_proximo_job(b) == job_id   # lease vencido: retomado
    assert worker.executar_job(job_id, b) == "concluido"

    assert len(faturas_ativas()) == 3             # nada perdido, nada duplicado
    assert contar(Fatura) == 3
    assert ambiente.demae.chamadas == 3
    tipos = [t for t, _ in _eventos(job_id)]
    assert tipos == ["progresso", "progresso", "progresso", "concluido"]
    with sessao() as s:
        job = s.get(Job, job_id)
        assert job.status == "concluido" and job.tentativas == 2
    assert [i.tentativas for i in _itens(job_id)] == [1, 2, 1]

    # O worker "morto" volta e tenta gravar: não pode.
    with pytest.raises(ingestao.LeasePerdida):
        ingestao.finalizar_job(job_id, a)


def test_dono_morto_no_mesmo_host_e_retomado_na_hora(ambiente):
    job_id = enviar(_tres_arquivos())
    host = worker._HOST
    with sessao(escrita=True) as s:  # dono = pid que não existe, heartbeat fresco
        s.execute(update(Job).where(Job.id == job_id).values(
            status="processando", worker_id=f"{host}:999999:deadbeef", heartbeat_em=agora()))
    b = worker.novo_worker_id()
    assert worker.pegar_proximo_job(b) == job_id


def test_item_que_derruba_o_worker_repetidamente_e_abandonado(ambiente):
    job_id = enviar(_tres_arquivos())
    primeiro = _itens(job_id)[0]
    for _ in range(ingestao.MAX_TENTATIVAS_ITEM):
        w = worker.novo_worker_id()
        _envelhecer_heartbeat(job_id)
        assert worker.pegar_proximo_job(w) == job_id
        ingestao.iniciar_item(primeiro.id, w)  # "crash" sempre no mesmo arquivo
    w = worker.novo_worker_id()
    _envelhecer_heartbeat(job_id)
    assert worker.pegar_proximo_job(w) == job_id
    assert worker.executar_job(job_id, w) == "concluido"
    status = [i.status for i in _itens(job_id)]
    assert status == ["erro", "ok", "ok"]


def test_dois_workers_nunca_pegam_o_mesmo_job(ambiente):
    jobs = {enviar([(f"demae/j{i}.pdf", texto_demae(f"FATURA={i};MES=01/2024;CONTA=900-1;TOTAL=1.00"))])
            for i in range(24)}
    pegos: list[str] = []
    trava = threading.Lock()
    inicio = threading.Barrier(4)

    def rodar_worker():
        wid = worker.novo_worker_id()
        inicio.wait()
        while True:
            jid = worker.pegar_proximo_job(wid)
            if jid is None:
                return
            with trava:
                pegos.append(jid)

    threads = [threading.Thread(target=rodar_worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert sorted(pegos) == sorted(jobs)  # todos pegos, nenhum duas vezes


def test_dois_workers_processando_em_paralelo(ambiente):
    for i in range(6):
        enviar([(f"demae/p{i}.pdf", texto_demae(f"FATURA={i};MES=01/2024;CONTA=900-1;TOTAL=1.00",
                                                 "FATURA=999;MES=01/2024;CONTA=900-1;TOTAL=9.00"))])
    threads = [threading.Thread(target=lambda: [None for _ in iter(lambda: worker.processar_proximo(), None)])
               for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    with sessao() as s:
        assert set(s.scalars(select(Job.status))) == {"concluido"}
    # a fatura 999 aparece nos 6 arquivos: uma só no banco
    assert len(faturas_ativas()) == 7


def test_consulta_do_postgres_usa_skip_locked():
    from sqlalchemy.dialects import postgresql

    q = select(Job.id).where(Job.status == "pendente").with_for_update(skip_locked=True, of=Job)
    assert "FOR UPDATE OF jobs SKIP LOCKED" in str(q.compile(dialect=postgresql.dialect()))


def test_parada_graciosa_devolve_job_para_a_fila(ambiente):
    job_id = enviar(_tres_arquivos())
    w = worker.novo_worker_id()
    assert worker.pegar_proximo_job(w) == job_id
    parar = threading.Event()
    parar.set()
    assert worker.executar_job(job_id, w, parar) == "devolvido"
    with sessao() as s:
        assert s.get(Job, job_id).status == "pendente"
    assert worker.processar_proximo() == job_id
