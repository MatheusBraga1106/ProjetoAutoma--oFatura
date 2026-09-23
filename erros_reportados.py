"""Fila de erros reportados pela aba "Erros" da UI — agora no mesmo banco das
faturas (tabela erros_reportados; ver banco/modelos.py). Guarda o relato do
usuário + a referência da fatura; quando a referência bate com a chave de
uma fatura do banco, liga as duas (fatura_id). Não altera nenhuma fatura —
é só uma fila de revisão manual.

As funções mantêm a assinatura e o formato de saída da versão SQLite antiga
(dicts com id, concessionaria, num_fatura, conta_dv, mes_ano_ref, mensagem,
status, data_criacao). Pra trazer os relatos do erros_reportados.db antigo:
    python -m banco.migrar --importar-erros-sqlite erros_reportados.db
"""

from datetime import datetime

from sqlalchemy import select

from banco.modelos import ErroReportado, Fatura
from banco.sessao import obter_engine, sessao

STATUS_VALIDOS = {"aberto", "resolvido"}


def inicializar_db() -> None:
    """Compatibilidade: o schema é criado por banco.migrar (automaticamente
    no primeiro uso do banco)."""
    obter_engine()


def _para_dict(erro: ErroReportado) -> dict:
    return {
        "id": erro.id,
        "concessionaria": erro.concessionaria,
        "num_fatura": erro.num_fatura,
        "conta_dv": erro.conta_dv,
        "mes_ano_ref": erro.mes_ano_ref,
        "mensagem": erro.mensagem,
        "status": erro.status,
        "data_criacao": erro.data_criacao,
        "fatura_id": erro.fatura_id,
    }


def _achar_fatura(s, concessionaria: str, num_fatura: str, conta_dv: str, mes_ano_ref: str) -> "int | None":
    if not concessionaria:
        return None
    from ingestao import chave_eh_completa, normalizar_mes_ano, normalizar_num_fatura
    from pipeline import normalizar_conta_dv

    empresa = concessionaria.strip().upper()
    nf, mes, conta = normalizar_num_fatura(num_fatura), normalizar_mes_ano(mes_ano_ref), normalizar_conta_dv(conta_dv)
    if not chave_eh_completa(empresa, nf, mes, conta):
        return None
    return s.scalar(select(Fatura.id).where(Fatura.chave_natural == f"{empresa}|{nf}|{mes}|{conta}"))


def criar_erro(mensagem: str, concessionaria: str = "", num_fatura: str = "",
               conta_dv: str = "", mes_ano_ref: str = "") -> dict:
    with sessao(escrita=True) as s:
        erro = ErroReportado(
            concessionaria=concessionaria.strip(), num_fatura=num_fatura.strip(), conta_dv=conta_dv.strip(),
            mes_ano_ref=mes_ano_ref.strip(), mensagem=mensagem.strip(), status="aberto",
            data_criacao=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        erro.fatura_id = _achar_fatura(s, erro.concessionaria, erro.num_fatura, erro.conta_dv, erro.mes_ano_ref)
        s.add(erro)
        s.flush()
        return _para_dict(erro)


def obter_erro(erro_id: int) -> "dict | None":
    with sessao() as s:
        erro = s.get(ErroReportado, erro_id)
        return _para_dict(erro) if erro else None


def listar_erros(status: "str | None" = None) -> list[dict]:
    with sessao() as s:
        consulta = select(ErroReportado).order_by(ErroReportado.id.desc())
        if status:
            consulta = consulta.where(ErroReportado.status == status)
        return [_para_dict(e) for e in s.scalars(consulta)]


def atualizar_status(erro_id: int, status: str) -> "dict | None":
    if status not in STATUS_VALIDOS:
        raise ValueError(f"status inválido: {status!r} (esperado um de {STATUS_VALIDOS})")
    with sessao(escrita=True) as s:
        erro = s.get(ErroReportado, erro_id)
        if erro is None:
            return None
        erro.status = status
        return _para_dict(erro)
