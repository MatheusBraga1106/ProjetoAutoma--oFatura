import os
import sys

import pytest

_AQUI = os.path.dirname(os.path.abspath(__file__))
_RAIZ = os.path.dirname(os.path.dirname(_AQUI))
for _p in (_RAIZ, _AQUI):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from apoio_banco import preparar_ambiente  # noqa: E402


@pytest.fixture
def ambiente(tmp_path, monkeypatch):
    import banco.sessao
    import banco.storage

    amb = preparar_ambiente(tmp_path, monkeypatch)
    yield amb
    banco.sessao.reiniciar()
    banco.storage.reiniciar()
