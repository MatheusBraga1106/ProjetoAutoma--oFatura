"""CLI do extrator: reextração completa e manutenção, sempre sobre o banco
(DATABASE_URL) e o storage (S3_* ou pasta local em dev). Não grava CSV —
CSV é exportação gerada do banco (--exportar-csv, ou pela UI/API).

Reextração completa a partir de uma pasta (o que o `python Main.py` antigo
fazia, agora idempotente e retomável):
    python Main.py                         # pasta padrão: dados_entrada/
    python Main.py --pasta /mnt/entrada

A partir de um prefixo do bucket (dentro do container no Dokploy, onde os
PDFs não estão em disco — copie a pasta pro bucket antes, ex.:
`mc cp --recursive dados_entrada/ silo/<bucket>/entrada/`):
    python Main.py --s3-prefixo entrada/

Depois de corrigir um extrator (só os arquivos daquele extrator são
reextraídos; o resto é pulado):
    python Main.py --reprocessar-banco [--empresa SANEAGO] [--forcar]

Outros:
    python Main.py --recalcular            # refaz contas.json/hidrômetro/SUSPEITO sem reextrair
    python Main.py --exportar-csv PASTA    # banco_dados_<empresa>.csv por distribuidora

Por padrão o job é processado aqui mesmo (com progresso no terminal e
visível na UI); --enfileirar só cria o job e deixa pro `python worker.py`.
Interromper (Ctrl+C) é seguro: rodar de novo retoma — o que já entrou fica, e
arquivo já processado com a mesma versão do extrator é pulado.

Opções de texto: por padrão, na leitura de pasta/prefixo, um .txt útil ao
lado do PDF (gerado por rodadas antigas do Main.py) é aproveitado como texto
do PDF — evita refazer ~200 OCRs. --ignorar-txt desliga; --refazer-texto
ignora também o cache do banco e roda pdftotext/OCR de novo.
"""

import argparse
import os
import sys

for _fluxo in (sys.stdout, sys.stderr):
    try:
        _fluxo.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except (AttributeError, ValueError):
        pass

from banco.config import PASTA_BASE  # noqa: E402


def _imprimir_evento(evento: dict) -> None:
    status = "ok " if evento.get("status") == "ok" else "ERRO"
    extra = evento.get("empresa") or ""
    detalhe = evento.get("detalhe")
    if detalhe:
        extra = f"{extra} — {detalhe}" if extra else detalhe
    print(f" [{evento.get('indice')}/{evento.get('total')}] {status} {evento.get('arquivo')}  {extra}")


def _executar_ou_enfileirar(job_id: "str | None", enfileirar: bool) -> int:
    import worker
    from banco.modelos import Job
    from banco.sessao import sessao

    if job_id is None:
        print("Nada a processar.")
        return 0
    if enfileirar:
        print(f"Job {job_id} criado na fila — o worker vai processar.")
        return 0
    worker_id = worker.novo_worker_id()
    if not worker.pegar_proximo_job(worker_id, job_id=job_id):
        print(f"Job {job_id} já está com outro worker; acompanhe pela UI.")
        return 0
    print(f"Processando job {job_id}...")
    status = worker.executar_job(job_id, worker_id, ao_evento=_imprimir_evento)
    with sessao() as s:
        job = s.get(Job, job_id)
        resultado = job.resultado or {}
    print("\n" + "=" * 70)
    print(f"Job {job_id}: {status}")
    for empresa, info in sorted((resultado.get("empresas") or {}).items()):
        print(f"  {empresa:16} novas={info['adicionadas']:5} atualizadas={info['atualizadas']:5} "
              f"duplicadas={info['duplicadas']:5} removidas={info['removidas']:4} "
              f"suspeitas={info['suspeitas']:4} sem_cadastro={info['linhas_sem_correspondencia']}")
    erros = [a for a in resultado.get("arquivos", []) if a.get("status") == "erro"]
    if erros:
        print(f"  {len(erros)} arquivo(s) com erro (detalhe na UI ou em job_arquivos).")
    print("=" * 70)
    return 0 if status == "concluido" else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Reextração/manutenção das faturas no banco.")
    origem = parser.add_mutually_exclusive_group()
    origem.add_argument("--pasta", help="pasta com os PDFs (default: dados_entrada/)")
    origem.add_argument("--s3-prefixo", help="prefixo do bucket com os PDFs (ex.: entrada/)")
    origem.add_argument("--reprocessar-banco", action="store_true",
                        help="reprocessa os arquivos que já estão no banco")
    origem.add_argument("--recalcular", action="store_true",
                        help="só recalcula derivados (contas.json, hidrômetro, SUSPEITO)")
    origem.add_argument("--exportar-csv", metavar="PASTA", help="exporta um CSV por distribuidora")
    parser.add_argument("pasta_posicional", nargs="?", help=argparse.SUPPRESS)
    parser.add_argument("--empresa", help="com --reprocessar-banco: só esta distribuidora")
    parser.add_argument("--forcar", action="store_true", help="reextrai mesmo sem mudança de versão")
    parser.add_argument("--refazer-texto", action="store_true", help="refaz pdftotext/OCR (ignora cache e .txt)")
    parser.add_argument("--ignorar-txt", action="store_true", help="não aproveita .txt ao lado dos PDFs")
    parser.add_argument("--enfileirar", action="store_true", help="só cria o job (o worker processa)")
    args = parser.parse_args(argv)

    import ingestao
    from banco.sessao import obter_engine, sessao

    obter_engine()  # cria/atualiza schema (idempotente)
    parametros = {"forcar": args.forcar, "refazer_texto": args.refazer_texto}
    usar_txt = not (args.ignorar_txt or args.refazer_texto)

    if args.recalcular:
        with sessao(escrita=True) as s:
            n = ingestao.recalcular_desatualizados(s, todos=True)
        print(f"Derivados recalculados em {n} fatura(s).")
        return 0

    if args.exportar_csv:
        from pipeline import EMPRESAS_CONHECIDAS

        os.makedirs(args.exportar_csv, exist_ok=True)
        with sessao() as s:
            for empresa in EMPRESAS_CONHECIDAS:
                conteudo = ingestao.exportar_csv_empresa(s, empresa)
                if conteudo is None:
                    continue
                destino = os.path.join(args.exportar_csv, f"banco_dados_{empresa.lower()}.csv")
                with open(destino, "wb") as f:
                    f.write(conteudo)
                print(f"  {destino}")
        return 0

    if args.reprocessar_banco:
        empresa = args.empresa.upper() if args.empresa else None
        return _executar_ou_enfileirar(ingestao.job_reprocessar_banco(empresa, parametros), args.enfileirar)

    if args.s3_prefixo:
        print(f"Lendo PDFs do storage em '{args.s3_prefixo}'...")
        job_id = ingestao.job_de_prefixo_storage(args.s3_prefixo, usar_txt, parametros, progresso=print)
        return _executar_ou_enfileirar(job_id, args.enfileirar)

    pasta = args.pasta or args.pasta_posicional or os.path.join(PASTA_BASE, "dados_entrada")
    if not os.path.isdir(pasta):
        print(f"Pasta não encontrada: {pasta}")
        return 2
    print(f"Lendo PDFs de '{pasta}'...")
    job_id = ingestao.job_de_pasta(pasta, usar_txt, parametros, progresso=print)
    return _executar_ou_enfileirar(job_id, args.enfileirar)


if __name__ == "__main__":
    sys.exit(main())
