"""Reextração com versão nova do extrator: atualiza com histórico; sem mudança
de código, não faz nada; chave que muda por correção vira obsoleta; contas.json
recalculável sem reextrair."""

import json

from apoio_banco import CONTAS_FICTICIAS, contar, enviar, faturas_ativas, rodar, texto_demae
from sqlalchemy import select

import ingestao
from banco.modelos import Fatura, FaturaVersao
from banco.sessao import sessao

PDF = texto_demae("FATURA=1001;MES=01/2024;CONTA=900-1;AGUA=50.00;TOTAL=50.00")


def _versoes(fatura_id):
    with sessao() as s:
        return list(s.scalars(select(FaturaVersao).where(FaturaVersao.fatura_id == fatura_id)
                              .order_by(FaturaVersao.versao)))


def test_versao_nova_do_extrator_atualiza_com_historico(ambiente):
    rodar(enviar([("demae/a.pdf", PDF)]))
    (f,) = faturas_ativas()
    assert (f.versao, f.versao_extrator, f.valor_total) == (1, "demae:v1", 50.0)

    # "Correção" do extrator: muda o valor e a versão do código.
    ambiente.demae.bonus = 7.0
    ambiente.versao["DEMAE"] = "demae:v2"
    r = rodar(ingestao.job_reprocessar_banco())
    assert r["empresas"]["DEMAE"]["atualizadas"] == 1
    (f,) = faturas_ativas()
    assert (f.versao, f.versao_extrator, f.valor_total) == (2, "demae:v2", 57.0)
    hist = _versoes(f.id)
    assert [(v.versao, v.versao_extrator, v.motivo, v.dados_extraidos["VALOR_TOTAL"]) for v in hist] == [
        (1, "demae:v1", "nova", 50.0), (2, "demae:v2", "reextracao", 57.0)]

    # Mesma versão de novo: nada é reextraído.
    chamadas = ambiente.demae.chamadas
    rodar(ingestao.job_reprocessar_banco())
    rodar(enviar([("demae/a.pdf", PDF)]))
    assert ambiente.demae.chamadas == chamadas
    assert contar(FaturaVersao) == 2


def test_versao_nova_com_mesmo_resultado_nao_cria_versao(ambiente):
    rodar(enviar([("demae/a.pdf", PDF)]))
    ambiente.versao["DEMAE"] = "demae:v2"
    r = rodar(ingestao.job_reprocessar_banco())
    assert r["empresas"]["DEMAE"]["duplicadas"] == 1
    (f,) = faturas_ativas()
    assert f.versao == 1 and f.versao_extrator == "demae:v2"  # confirmada pela versão nova
    assert contar(FaturaVersao) == 1


def test_correcao_que_muda_a_chave_deixa_a_antiga_obsoleta(ambiente):
    ambiente.demae.ignorar_mes = True  # extrator "bugado": não lê o mês
    rodar(enviar([("demae/a.pdf", PDF)]))
    (antiga,) = faturas_ativas()
    assert antiga.mes_ano_ref == ""

    ambiente.demae.ignorar_mes = False
    ambiente.versao["DEMAE"] = "demae:v2"
    r = rodar(ingestao.job_reprocessar_banco())
    assert r["empresas"]["DEMAE"]["adicionadas"] == 1
    assert r["empresas"]["DEMAE"]["removidas"] == 1
    (nova,) = faturas_ativas()
    assert nova.mes_ano_ref == "01/2024"
    assert contar(Fatura) == 2 and contar(Fatura, Fatura.ativa.is_(False)) == 1  # histórico preservado


def test_duas_origens_divergentes_nao_ficam_trocando_versao(ambiente):
    rodar(enviar([("demae/a.pdf", PDF),
                  ("demae/b.pdf", texto_demae("FATURA=1001;MES=01/2024;CONTA=900-1;AGUA=55.00;TOTAL=55.00"))]))
    (f,) = faturas_ativas()
    assert f.versao == 2 and f.valor_total == 55.0  # o mais recente vence, o outro fica no histórico
    rodar(ingestao.job_reprocessar_banco(parametros={"forcar": True}))
    rodar(ingestao.job_reprocessar_banco(parametros={"forcar": True}))
    (f,) = faturas_ativas()
    assert f.versao == 2


def test_contas_json_recalculado_sem_reextrair(ambiente):
    rodar(enviar([("demae/a.pdf", PDF)]))
    (f,) = faturas_ativas()
    assert f.dados["UNIDADE JUDICIÁRIA"] == "Comarca Demae" and not f.suspeito

    contas = [dict(c) for c in CONTAS_FICTICIAS]
    contas[2]["UNIDADE JUDICIÁRIA"] = "Comarca Renomeada"
    contas[2]["AGUA"], contas[2]["ESGOTO"] = False, True  # água passa a ser zerada -> soma não fecha
    ambiente.contas.write_text(json.dumps(contas, ensure_ascii=False), encoding="utf-8")

    chamadas = ambiente.demae.chamadas
    with sessao(escrita=True) as s:
        assert ingestao.recalcular_desatualizados(s) == 1
    assert ambiente.demae.chamadas == chamadas
    (f,) = faturas_ativas()
    assert f.dados["UNIDADE JUDICIÁRIA"] == "Comarca Renomeada"
    assert f.dados["VALOR_AGUA"] == 0.0 and f.suspeito is True
    assert f.dados_extraidos["VALOR_AGUA"] == 50.0  # o extraído nunca é tocado
    assert f.versao == 1
    with sessao(escrita=True) as s:
        assert ingestao.recalcular_desatualizados(s) == 0
