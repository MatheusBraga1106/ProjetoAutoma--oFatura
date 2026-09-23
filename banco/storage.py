"""Object storage dos PDFs (e do cache de texto extraído).

Produção: qualquer S3-compatível (MinIO no Dokploy) via boto3 — mesmo
cliente genérico do hub-faturas. Sem S3_BUCKET configurado, cai numa pasta
local (<DADOS_SAIDA_DIR>/armazenamento) — só pra desenvolvimento; a UI avisa
no /pipeline/status-sistema.

Chaves são endereçadas por conteúdo (sha256), então gravar de novo o mesmo
PDF é no-op e nunca existem dois blobs iguais.
"""

import os
import threading

from banco.config import pasta_saida


def chave_pdf(sha256: str) -> str:
    return f"pdfs/{sha256[:2]}/{sha256}.pdf"


def chave_texto(sha256_pdf: str) -> str:
    return f"textos/{sha256_pdf[:2]}/{sha256_pdf}.txt"


class ArmazenamentoLocal:
    tipo = "local"

    def __init__(self, pasta: str):
        self.pasta = pasta

    def _caminho(self, chave: str) -> str:
        partes = [p for p in chave.split("/") if p not in ("", ".", "..")]
        return os.path.join(self.pasta, *partes)

    def garantir_bucket(self) -> None:
        os.makedirs(self.pasta, exist_ok=True)

    def existe(self, chave: str) -> bool:
        return os.path.exists(self._caminho(chave))

    def salvar(self, chave: str, conteudo: bytes, content_type: str = "application/octet-stream") -> None:
        destino = self._caminho(chave)
        os.makedirs(os.path.dirname(destino), exist_ok=True)
        temporario = destino + f".tmp{threading.get_ident()}"
        with open(temporario, "wb") as f:
            f.write(conteudo)
        os.replace(temporario, destino)

    def ler(self, chave: str) -> bytes:
        with open(self._caminho(chave), "rb") as f:
            return f.read()

    def listar(self, prefixo: str) -> list[str]:
        chaves = []
        for raiz, _dirs, arquivos in os.walk(self.pasta):
            for nome in arquivos:
                rel = os.path.relpath(os.path.join(raiz, nome), self.pasta).replace("\\", "/")
                if rel.startswith(prefixo) and ".tmp" not in nome:
                    chaves.append(rel)
        return sorted(chaves)

    def descricao(self) -> str:
        return f"local:{self.pasta}"


class ArmazenamentoS3:
    tipo = "s3"

    def __init__(self, bucket: str, endpoint_url: "str | None", access_key: str, secret_key: str, regiao: str):
        import boto3
        from botocore.client import Config

        self.bucket = bucket
        self.endpoint_url = endpoint_url
        self._cliente = boto3.client(
            "s3",
            endpoint_url=endpoint_url or None,
            aws_access_key_id=access_key or None,
            aws_secret_access_key=secret_key or None,
            region_name=regiao,
            # path-style: MinIO atrás de um domínio só (sem wildcard DNS de bucket)
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )
        self._bucket_ok = False

    def garantir_bucket(self) -> None:
        """Confere se o bucket existe e é acessível (head_bucket). NÃO cria:
        em produção a credencial da app é restrita ao bucket e não tem
        permissão de criar — quem cria é o deploy. S3_CRIAR_BUCKET=1 libera
        a criação (útil em dev contra um MinIO local)."""
        if self._bucket_ok:
            return
        from botocore.exceptions import ClientError

        try:
            self._cliente.head_bucket(Bucket=self.bucket)
        except ClientError as e:
            codigo = e.response.get("Error", {}).get("Code", "?")
            if os.environ.get("S3_CRIAR_BUCKET", "").strip() in ("1", "true") and codigo in ("404", "NoSuchBucket"):
                self._cliente.create_bucket(Bucket=self.bucket)
            else:
                raise RuntimeError(
                    f"Bucket S3 '{self.bucket}' inacessível em {self.endpoint_url or 'AWS'} "
                    f"(head_bucket -> {codigo}). Confira S3_BUCKET/S3_ENDPOINT_URL/credenciais; "
                    f"o bucket precisa existir antes (a app não cria bucket)."
                ) from e
        self._bucket_ok = True

    def listar(self, prefixo: str) -> list[str]:
        chaves = []
        paginador = self._cliente.get_paginator("list_objects_v2")
        for pagina in paginador.paginate(Bucket=self.bucket, Prefix=prefixo):
            chaves.extend(obj["Key"] for obj in pagina.get("Contents", []))
        return chaves

    def existe(self, chave: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._cliente.head_object(Bucket=self.bucket, Key=chave)
            return True
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    def salvar(self, chave: str, conteudo: bytes, content_type: str = "application/octet-stream") -> None:
        self.garantir_bucket()
        self._cliente.put_object(Bucket=self.bucket, Key=chave, Body=conteudo, ContentType=content_type)

    def ler(self, chave: str) -> bytes:
        return self._cliente.get_object(Bucket=self.bucket, Key=chave)["Body"].read()

    def descricao(self) -> str:
        return f"s3:{self.endpoint_url or 'aws'}/{self.bucket}"


_cache: dict[tuple, object] = {}
_lock = threading.Lock()


def obter_armazenamento():
    bucket = os.environ.get("S3_BUCKET", "").strip()
    if bucket:
        chave = (
            "s3", bucket, os.environ.get("S3_ENDPOINT_URL", ""), os.environ.get("S3_ACCESS_KEY", ""),
            os.environ.get("S3_SECRET_KEY", ""), os.environ.get("S3_REGION", "") or "us-east-1",
        )
    else:
        pasta = os.environ.get("ARMAZENAMENTO_LOCAL_DIR") or os.path.join(pasta_saida(), "armazenamento")
        chave = ("local", pasta)
    with _lock:
        armazenamento = _cache.get(chave)
        if armazenamento is None:
            if chave[0] == "s3":
                armazenamento = ArmazenamentoS3(chave[1], chave[2] or None, chave[3], chave[4], chave[5])
            else:
                armazenamento = ArmazenamentoLocal(chave[1])
            _cache[chave] = armazenamento
        return armazenamento


def reiniciar() -> None:
    with _lock:
        _cache.clear()
