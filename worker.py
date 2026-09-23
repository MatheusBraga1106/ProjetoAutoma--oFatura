"""Worker de jobs: fila = tabela `jobs`, por polling (sem Redis).

    python worker.py              # loop: pega job, processa, repete
    python worker.py --uma-vez    # processa o que estiver na fila e sai

Como um job é "pego" com segurança:
  - Postgres: SELECT ... FOR UPDATE SKIP LOCKED nos candidatos + UPDATE
    condicional (compare-and-swap no status/worker_id) — dois workers nunca
    ficam com o mesmo job, e nenhum espera pelo outro.
  - SQLite (dev/testes): não tem SKIP LOCKED; a transação de escrita abre com
    BEGIN IMMEDIATE (um escritor por vez) e o mesmo UPDATE condicional
    garante que só um ganha.

Retomada: o worker atualiza `heartbeat_em` a cada poucos segundos numa
thread. Job 'processando' sem heartbeat há mais de JOB_LEASE_SEGUNDOS
(default 90) é considerado órfão e outro worker retoma — a partir dos itens
ainda pendentes (cada arquivo concluído já está gravado). No mesmo host, se o
processo dono comprovadamente morreu (pid), a retomada é imediata.
"""

from __future__ import annotations

import argparse
import os
import signal
import socket
import sys
import threading
import time
import traceback
import uuid
from datetime import timedelta

from sqlalchemy import and_, func, or_, select, update

import ingestao
from banco.config import lease_job_segundos, max_tentativas_job
from banco.modelos import Job, JobArquivo, agora
from banco.sessao import sessao

_HOST = socket.gethostname()
_IDS_DESTE_PROCESSO: set[str] = set()


def novo_worker_id() -> str:
    wid = f"{_HOST}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    _IDS_DESTE_PROCESSO.add(wid)
    return wid


def _pid_vivo(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) no Windows MATA o processo — usar a API do kernel.
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.GetLastError() == 5  # acesso negado = existe
        try:
            codigo = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(handle, ctypes.byref(codigo))
            return codigo.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def dono_comprovadamente_morto(worker_id: "str | None") -> bool:
    """True só quando dá pra ter certeza: o dono é deste mesmo host e o
    processo dele não existe mais (ou é este processo, numa encarnação
    anterior — caso do container reiniciado com o mesmo pid 1)."""
    if not worker_id:
        return False
    partes = worker_id.rsplit(":", 2)
    if len(partes) != 3 or partes[0] != _HOST:
        return False
    try:
        pid = int(partes[1])
    except ValueError:
        return False
    if pid == os.getpid():
        return worker_id not in _IDS_DESTE_PROCESSO
    return not _pid_vivo(pid)


def pegar_proximo_job(worker_id: str, job_id: "str | None" = None) -> "str | None":
    """Reserva um job pra este worker (pendente, ou órfão). Devolve o id ou None."""
    with sessao(escrita=True) as s:
        postgres = s.get_bind().dialect.name == "postgresql"
        limite = agora() - timedelta(seconds=lease_job_segundos())

        def consulta(*condicoes):
            q = select(Job.id, Job.status, Job.worker_id).where(*condicoes)
            if job_id:
                q = q.where(Job.id == job_id)
            q = q.order_by(Job.criado_em).limit(20)
            if postgres:
                q = q.with_for_update(skip_locked=True, of=Job)
            return list(s.execute(q))

        candidatos = consulta(or_(
            Job.status == "pendente",
            and_(Job.status == "processando", or_(Job.heartbeat_em.is_(None), Job.heartbeat_em < limite)),
        ))
        candidatos += [c for c in consulta(Job.status == "processando", Job.heartbeat_em >= limite,
                                           Job.worker_id.like(f"{_HOST}:%"))
                       if dono_comprovadamente_morto(c.worker_id)]

        for c in candidatos:
            cas = [Job.id == c.id, Job.status == c.status,
                   Job.worker_id.is_(None) if c.worker_id is None else Job.worker_id == c.worker_id]
            r = s.execute(
                update(Job).where(*cas).values(
                    status="processando", worker_id=worker_id, heartbeat_em=agora(),
                    iniciado_em=func.coalesce(Job.iniciado_em, agora()), tentativas=Job.tentativas + 1,
                ).execution_options(synchronize_session=False)
            )
            if r.rowcount != 1:
                continue
            job = s.get(Job, c.id, populate_existing=True)
            if job.tentativas > max_tentativas_job():
                mensagem = f"job abandonado após {job.tentativas - 1} tentativas sem terminar"
                job.status, job.erro_mensagem, job.resultado, job.concluido_em = "erro", mensagem, {"erro": mensagem}, agora()
                ingestao.registrar_evento(s, job.id, "erro", {"erro": mensagem})
                continue
            return c.id
    return None


class Heartbeat(threading.Thread):
    def __init__(self, job_id: str, worker_id: str):
        super().__init__(daemon=True, name=f"heartbeat-{job_id[:8]}")
        self.job_id, self.worker_id = job_id, worker_id
        self.intervalo = max(1.0, min(10.0, lease_job_segundos() / 3))
        self.parar = threading.Event()
        self.perdido = threading.Event()

    def run(self):
        while not self.parar.wait(self.intervalo):
            try:
                with sessao(escrita=True) as s:
                    r = s.execute(
                        update(Job).where(Job.id == self.job_id, Job.worker_id == self.worker_id,
                                          Job.status == "processando")
                        .values(heartbeat_em=agora()).execution_options(synchronize_session=False)
                    )
                    if r.rowcount == 0:
                        self.perdido.set()
                        return
            except Exception:  # noqa: BLE001 — banco instável: tenta de novo no próximo ciclo
                traceback.print_exc()


def _marcar_item_erro(item_id: int, worker_id: str, mensagem: str) -> None:
    with sessao(escrita=True) as s:
        it = s.get(JobArquivo, item_id)
        job = s.get(Job, it.job_id)
        if it.status != "pendente" or job.worker_id != worker_id:
            return
        it.status, it.detalhe, it.processado_em = "erro", mensagem, agora()
        job.arquivos_processados = (job.arquivos_processados or 0) + 1
        ingestao.registrar_evento(s, job.id, "progresso", {
            "arquivo": it.caminho_origem, "indice": it.indice, "total": job.total_arquivos,
            "status": "erro", "detalhe": mensagem,
        })


def _devolver_job(job_id: str, worker_id: str) -> None:
    """Parada graciosa no meio do job: devolve pra fila na hora (sem esperar
    o lease vencer)."""
    with sessao(escrita=True) as s:
        s.execute(update(Job).where(Job.id == job_id, Job.worker_id == worker_id, Job.status == "processando")
                  .values(status="pendente", worker_id=None, heartbeat_em=None)
                  .execution_options(synchronize_session=False))


def executar_job(job_id: str, worker_id: str, parar: "threading.Event | None" = None,
                 ao_evento=None) -> str:
    heartbeat = Heartbeat(job_id, worker_id)
    heartbeat.start()
    try:
        with sessao() as s:
            opcoes = ingestao.Opcoes.de_parametros(s.get(Job, job_id).parametros)
        while True:
            if heartbeat.perdido.is_set():
                raise ingestao.LeasePerdida(job_id)
            if parar is not None and parar.is_set():
                _devolver_job(job_id, worker_id)
                return "devolvido"
            with sessao() as s:
                proximo = s.scalar(select(JobArquivo.id).where(
                    JobArquivo.job_id == job_id, JobArquivo.status == "pendente").order_by(JobArquivo.indice).limit(1))
            if proximo is None:
                break
            try:
                evento = ingestao.processar_item(proximo, worker_id, opcoes)
                if ao_evento is not None and evento:
                    ao_evento(evento)
            except ingestao.LeasePerdida:
                raise
            except Exception as e:  # noqa: BLE001 — falha inesperada num arquivo não derruba o job
                traceback.print_exc()
                _marcar_item_erro(proximo, worker_id, f"falha interna: {e}")
        ingestao.finalizar_job(job_id, worker_id)
        return "concluido"
    except ingestao.LeasePerdida:
        print(f"[worker] job {job_id} foi retomado por outro worker; abandonando.", flush=True)
        return "lease_perdida"
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        ingestao.marcar_job_erro(job_id, worker_id, str(e) or e.__class__.__name__)
        return "erro"
    finally:
        heartbeat.parar.set()


def processar_proximo(worker_id: "str | None" = None, parar: "threading.Event | None" = None) -> "str | None":
    worker_id = worker_id or novo_worker_id()
    job_id = pegar_proximo_job(worker_id)
    if job_id is None:
        return None
    executar_job(job_id, worker_id, parar)
    return job_id


def executar_job_se_disponivel(job_id: str) -> None:
    """Usado pela API (WORKER_EMBUTIDO) logo após criar o job: tenta
    reservá-lo e processar na hora. Se outro worker já pegou, não faz nada."""
    worker_id = novo_worker_id()
    try:
        if pegar_proximo_job(worker_id, job_id=job_id):
            executar_job(job_id, worker_id)
    except Exception:  # noqa: BLE001
        traceback.print_exc()


def _manutencao() -> None:
    """Quando ocioso: recalcula derivados que ficaram velhos (contas.json ou
    pipeline.py mudaram) — sem reextrair nada."""
    with sessao(escrita=True) as s:
        n = ingestao.recalcular_desatualizados(s)
    if n:
        print(f"[worker] derivados recalculados em {n} fatura(s) (contas.json/pipeline mudaram)", flush=True)


def loop(parar: "threading.Event | None" = None, intervalo: "float | None" = None) -> None:
    parar = parar or threading.Event()
    intervalo = intervalo if intervalo is not None else float(os.environ.get("WORKER_INTERVALO_SEGUNDOS", "2"))
    worker_id = novo_worker_id()
    ultima_manutencao = 0.0
    while not parar.is_set():
        try:
            if processar_proximo(worker_id, parar):
                continue
            if time.monotonic() - ultima_manutencao > 60:
                ultima_manutencao = time.monotonic()
                _manutencao()
        except Exception:  # noqa: BLE001 — banco fora do ar etc.: espera e tenta de novo
            traceback.print_exc()
        parar.wait(intervalo)


def iniciar_embutido() -> tuple[threading.Thread, threading.Event]:
    parar = threading.Event()
    thread = threading.Thread(target=loop, args=(parar,), daemon=True, name="worker-embutido")
    thread.start()
    return thread, parar


def main(argv=None) -> int:
    for fluxo in (sys.stdout, sys.stderr):
        try:
            fluxo.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description="Worker de jobs de extração.")
    parser.add_argument("--uma-vez", action="store_true", help="esvazia a fila e sai")
    args = parser.parse_args(argv)

    from banco.migrar import migrar
    from banco.sessao import obter_engine
    from banco.storage import obter_armazenamento

    migrar(obter_engine())
    try:
        obter_armazenamento().garantir_bucket()  # head_bucket; nunca cria
    except RuntimeError as e:
        print(f"[worker] {e}", file=sys.stderr, flush=True)
        return 3
    parar = threading.Event()

    def _sinal(_signum, _frame):
        print("[worker] sinal recebido; terminando o arquivo atual e devolvendo o job à fila...", flush=True)
        parar.set()

    signal.signal(signal.SIGTERM, _sinal)
    signal.signal(signal.SIGINT, _sinal)

    if args.uma_vez:
        worker_id = novo_worker_id()
        while not parar.is_set() and processar_proximo(worker_id, parar):
            pass
        return 0
    print(f"[worker] iniciado ({_HOST}, pid {os.getpid()}); aguardando jobs...", flush=True)
    loop(parar)
    return 0


if __name__ == "__main__":
    sys.exit(main())
