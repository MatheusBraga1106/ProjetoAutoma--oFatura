"""Schema do banco do extrator de faturas.

Mesmo desenho do projeto irmão hub-faturas (arquivos_upload, jobs,
job_arquivos, faturas com colunas fixas + `dados` JSON + `versao`), com o
que este extrator precisa a mais:

  arquivos_upload     1 linha por CONTEÚDO de PDF (sha256 único). O blob vai
                      pro object storage uma vez só.
  arquivo_origens     todos os caminhos/nomes pelos quais aquele mesmo
                      conteúdo já chegou (a distribuidora é identificada pelo
                      caminho, então nenhum é descartado).
  jobs / job_arquivos fila persistida: o worker pega o job e processa item a
  / job_eventos       item; cada item concluído fica gravado (retomável) e
                      gera um evento que o SSE da web lê do banco.
  faturas             1 linha por fatura (chave natural única), com a saída
                      crua do extrator (`dados_extraidos`) separada da linha
                      final exibida (`dados` = extraídos + hidrômetro +
                      contas.json + SUSPEITO), que é recalculável.
  fatura_versoes      histórico: cada conteúdo diferente extraído pra uma
                      fatura vira uma versão, com a versão do código que o
                      produziu.
  fatura_origens      quais arquivos produziram cada fatura (e com qual
                      conteúdo) — base da regra "fatura obsoleta" quando uma
                      reextração deixa de produzir uma chave.
  hidrometros_analitica  linhas da SANEAGO analítica, persistidas pra casar
                      com faturas SANEAGO de qualquer lote (antes ou depois).
  erros_reportados    fila da aba Erros (antes num SQLite à parte).
  meta                chave/valor interno (versão do schema).

Datas são gravadas em UTC "ingênuo" (sem tzinfo) — o SQLite não guarda fuso
e misturar aware/naive quebra comparação de heartbeat.
"""

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

JSONTipo = JSON().with_variant(JSONB(), "postgresql")


def agora() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Meta(Base):
    __tablename__ = "meta"

    chave: Mapped[str] = mapped_column(String(100), primary_key=True)
    valor: Mapped[str] = mapped_column(Text)


class ArquivoUpload(Base):
    __tablename__ = "arquivos_upload"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    tamanho_bytes: Mapped[int] = mapped_column(BigInteger)
    chave_storage: Mapped[str] = mapped_column(String(500))
    # Distribuidora fixada pela 1ª origem que conseguiu identificar. Origens
    # posteriores que apontem outra ficam registradas em arquivo_origens.
    empresa: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)

    # Cache do texto (pdftotext/OCR) no storage — OCR é o passo caro, e os
    # extratores mudam muito mais que ele. Reextrair reaproveita este texto.
    texto_chave_storage: Mapped[str | None] = mapped_column(String(500), nullable=True)
    texto_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    texto_via_ocr: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    texto_fonte: Mapped[str | None] = mapped_column(String(20), nullable=True)  # pdftotext|ocr|txt_existente
    texto_versao: Mapped[str | None] = mapped_column(String(40), nullable=True)

    # Estado do último processamento.
    # pendente | ok | sem_dados | erro | nao_identificado | nao_implementado
    status: Mapped[str] = mapped_column(String(30), default="pendente")
    versao_extrator: Mapped[str | None] = mapped_column(String(80), nullable=True)
    texto_sha256_processado: Mapped[str | None] = mapped_column(String(64), nullable=True)
    linhas_extraidas: Mapped[int | None] = mapped_column(Integer, nullable=True)
    erro_mensagem: Mapped[str | None] = mapped_column(Text, nullable=True)
    processado_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=agora)


class ArquivoOrigem(Base):
    __tablename__ = "arquivo_origens"
    __table_args__ = (UniqueConstraint("arquivo_id", "caminho_origem", name="uq_origem_arquivo_caminho"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    arquivo_id: Mapped[int] = mapped_column(ForeignKey("arquivos_upload.id"), index=True)
    caminho_origem: Mapped[str] = mapped_column(String(1000))
    nome_arquivo: Mapped[str] = mapped_column(String(500))
    pasta_origem: Mapped[str] = mapped_column(String(1000), default="")
    empresa_identificada: Mapped[str | None] = mapped_column(String(40), nullable=True)
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=agora)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_status_criado", "status", "criado_em"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # uuid4().hex, igual ao contrato antigo
    tipo: Mapped[str] = mapped_column(String(30), default="extracao")  # extracao | reextracao
    # pendente | processando | concluido | erro
    status: Mapped[str] = mapped_column(String(20), default="pendente")
    parametros: Mapped[dict] = mapped_column(JSONTipo, default=dict)
    total_arquivos: Mapped[int] = mapped_column(Integer, default=0)
    arquivos_processados: Mapped[int] = mapped_column(Integer, default=0)
    resultado: Mapped[dict | None] = mapped_column(JSONTipo, nullable=True)
    erro_mensagem: Mapped[str | None] = mapped_column(Text, nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    tentativas: Mapped[int] = mapped_column(Integer, default=0)
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=agora)
    iniciado_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    heartbeat_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    concluido_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class JobArquivo(Base):
    __tablename__ = "job_arquivos"
    __table_args__ = (UniqueConstraint("job_id", "indice", name="uq_job_arquivo_indice"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    indice: Mapped[int] = mapped_column(Integer)  # 1-based, ordem de envio
    arquivo_id: Mapped[int] = mapped_column(ForeignKey("arquivos_upload.id"), index=True)
    origem_id: Mapped[int] = mapped_column(ForeignKey("arquivo_origens.id"))
    caminho_origem: Mapped[str] = mapped_column(String(1000))
    status: Mapped[str] = mapped_column(String(20), default="pendente")  # pendente | ok | erro
    empresa: Mapped[str | None] = mapped_column(String(40), nullable=True)
    detalhe: Mapped[str | None] = mapped_column(Text, nullable=True)
    resultado: Mapped[dict | None] = mapped_column(JSONTipo, nullable=True)
    # Quantas vezes um worker COMEÇOU este item. Se o processo morre no meio
    # de um arquivo (OCR estourando memória, por ex.), o item volta pra fila
    # na retomada; passou do limite, é marcado erro pra não travar o job.
    tentativas: Mapped[int] = mapped_column(Integer, default=0)
    processado_em: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class JobEvento(Base):
    __tablename__ = "job_eventos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    tipo: Mapped[str] = mapped_column(String(20))  # progresso | concluido | erro
    dados: Mapped[dict] = mapped_column(JSONTipo, default=dict)
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=agora)


class Fatura(Base):
    __tablename__ = "faturas"
    __table_args__ = (
        Index("ix_faturas_empresa_ativa", "empresa", "ativa"),
        Index("ix_faturas_conta_empresa", "conta_normalizada", "empresa"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # EMPRESA|NUM_FATURA|MM/AAAA|CONTA (normalizados) — ver ingestao.chave_natural
    chave_natural: Mapped[str] = mapped_column(String(600), unique=True)
    empresa: Mapped[str] = mapped_column(String(40))
    num_fatura: Mapped[str] = mapped_column(String(100), default="")
    mes_ano_ref: Mapped[str] = mapped_column(String(20), default="")
    conta_dv: Mapped[str] = mapped_column(String(100), default="")
    conta_normalizada: Mapped[str] = mapped_column(String(100), default="")
    competencia: Mapped[str | None] = mapped_column(String(7), nullable=True)  # AAAA-MM, pra ordenar
    chave_completa: Mapped[bool] = mapped_column(Boolean, default=True)
    ativa: Mapped[bool] = mapped_column(Boolean, default=True)

    versao: Mapped[int] = mapped_column(Integer, default=1)
    versao_extrator: Mapped[str] = mapped_column(String(80))
    hash_conteudo: Mapped[str] = mapped_column(String(64))
    arquivo_id: Mapped[int] = mapped_column(ForeignKey("arquivos_upload.id"))
    dados_extraidos: Mapped[dict] = mapped_column(JSONTipo, default=dict)

    # Derivados (recalculáveis sem reextrair): hidrômetro, contas.json, SUSPEITO.
    dados: Mapped[dict] = mapped_column(JSONTipo, default=dict)
    valor_total: Mapped[float | None] = mapped_column(Numeric(14, 2, asdecimal=False), nullable=True)
    consumo_m3: Mapped[float | None] = mapped_column(Float, nullable=True)
    num_hidrometro: Mapped[str | None] = mapped_column(String(100), nullable=True)
    suspeito: Mapped[bool] = mapped_column(Boolean, default=False)
    texto_busca: Mapped[str] = mapped_column(Text, default="")
    hash_derivados: Mapped[str | None] = mapped_column(String(64), nullable=True)

    criado_em: Mapped[datetime] = mapped_column(DateTime, default=agora)
    atualizado_em: Mapped[datetime] = mapped_column(DateTime, default=agora)


class FaturaVersao(Base):
    __tablename__ = "fatura_versoes"
    __table_args__ = (UniqueConstraint("fatura_id", "versao", name="uq_fatura_versao"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fatura_id: Mapped[int] = mapped_column(ForeignKey("faturas.id"), index=True)
    versao: Mapped[int] = mapped_column(Integer)
    arquivo_id: Mapped[int] = mapped_column(ForeignKey("arquivos_upload.id"))
    job_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    versao_extrator: Mapped[str] = mapped_column(String(80))
    hash_conteudo: Mapped[str] = mapped_column(String(64))
    dados_extraidos: Mapped[dict] = mapped_column(JSONTipo, default=dict)
    # nova | reextracao | outra_origem | reativada
    motivo: Mapped[str] = mapped_column(String(30))
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=agora)


class FaturaOrigem(Base):
    __tablename__ = "fatura_origens"

    fatura_id: Mapped[int] = mapped_column(ForeignKey("faturas.id"), primary_key=True)
    arquivo_id: Mapped[int] = mapped_column(ForeignKey("arquivos_upload.id"), primary_key=True, index=True)
    hash_conteudo: Mapped[str] = mapped_column(String(64))
    versao_extrator: Mapped[str] = mapped_column(String(80))
    ativa: Mapped[bool] = mapped_column(Boolean, default=True)
    atualizado_em: Mapped[datetime] = mapped_column(DateTime, default=agora)


class HidrometroAnalitica(Base):
    __tablename__ = "hidrometros_analitica"
    __table_args__ = (
        UniqueConstraint("arquivo_id", "conta_normalizada", "mes_ano_ref", name="uq_hidrometro_arquivo_conta_mes"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    arquivo_id: Mapped[int] = mapped_column(ForeignKey("arquivos_upload.id"), index=True)
    conta_dv: Mapped[str] = mapped_column(String(100))
    conta_normalizada: Mapped[str] = mapped_column(String(100), index=True)
    mes_ano_ref: Mapped[str] = mapped_column(String(20), default="")
    competencia: Mapped[str | None] = mapped_column(String(7), nullable=True)
    num_hidrometro: Mapped[str] = mapped_column(String(100))
    versao_extrator: Mapped[str] = mapped_column(String(80))
    ativa: Mapped[bool] = mapped_column(Boolean, default=True)
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=agora)


class ErroReportado(Base):
    __tablename__ = "erros_reportados"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    concessionaria: Mapped[str] = mapped_column(String(60), default="")
    num_fatura: Mapped[str] = mapped_column(String(100), default="")
    conta_dv: Mapped[str] = mapped_column(String(100), default="")
    mes_ano_ref: Mapped[str] = mapped_column(String(20), default="")
    mensagem: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="aberto")
    # Texto "AAAA-MM-DD HH:MM:SS" (hora local), mesmo formato do SQLite antigo
    # — a UI mostra esse campo como veio.
    data_criacao: Mapped[str] = mapped_column(String(19))
    # Ligação com a fatura, quando a referência informada bate com uma chave.
    fatura_id: Mapped[int | None] = mapped_column(ForeignKey("faturas.id"), nullable=True)
