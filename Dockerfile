# Imagem única do extrator de faturas de água. A MESMA imagem roda três
# papéis no docker-compose.yml (ver lá): migração (python -m banco.migrar),
# app web (CMD abaixo) e worker (python worker.py).
#
# Base fixada em Debian 13 "trixie" (e não só "3.12-slim", que muda de
# Debian por baixo sem avisar): os nomes/versões dos pacotes apt abaixo
# foram conferidos pra trixie — tesseract-ocr 5.5.0, tesseract-ocr-por
# 1:4.1.0, poppler-utils 25.03.
FROM python:3.12-slim-trixie

# UTF-8 em tudo: o pipeline lê/grava .txt com acento e o pdftotext é
# chamado com -enc UTF-8; sem locale UTF-8 o Python/stdout do container
# cai em ASCII e quebra log de nome de arquivo com "Ç", "Ã" etc.
ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONIOENCODING=utf-8 \
    PYTHONUTF8=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8000

# poppler-utils -> pdftotext (1ª tentativa, PDF com camada de texto)
# tesseract-ocr + tesseract-ocr-por -> OCR (2ª tentativa, PDF-imagem)
# A verificação no fim FALHA O BUILD se o pacote de português não estiver
# registrado ou se algum dos dois binários não estiver no PATH — melhor
# descobrir aqui do que numa fatura escaneada em produção.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        poppler-utils \
        tesseract-ocr \
        tesseract-ocr-por \
 && rm -rf /var/lib/apt/lists/* \
 && tesseract --version \
 && tesseract --list-langs 2>&1 | tee /tmp/langs.txt \
 && grep -qx "por" /tmp/langs.txt \
 && rm /tmp/langs.txt \
 && command -v pdftotext \
 && pdftotext -v

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Código como root (somente leitura pro usuário da app); só /app/var é
# gravável. /tmp (uploads e .txt temporários do pipeline) já é gravável.
COPY . .

# Usuário sem privilégio. UID fixo pra facilitar permissão de volume se um
# dia montar algo em /app/var.
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --home-dir /home/app --create-home --shell /usr/sbin/nologin app \
 && mkdir -p /app/var/dados_saida \
 && chown -R app:app /app/var

# Transitório: enquanto api.py ainda grava CSV (DADOS_SAIDA_DIR) e SQLite
# da aba Erros (ERROS_DB_PATH), eles vão pra uma pasta gravável pelo
# usuário não-root. NÃO é persistente (some a cada redeploy) — a fonte de
# verdade em produção é o Postgres + storage S3 (ver docs/decisao-armazenamento.md).
ENV DADOS_SAIDA_DIR=/app/var/dados_saida \
    ERROS_DB_PATH=/app/var/erros_reportados.db

USER app

EXPOSE 8000

# Healthcheck do papel "app web". O docker-compose.yml sobrescreve isto
# no worker (que não serve HTTP) e desliga na migração (one-shot).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/health', timeout=3)" || exit 1

CMD ["sh", "-c", "exec uvicorn api:app --host 0.0.0.0 --port ${PORT:-8000}"]
