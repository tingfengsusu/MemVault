import sys
from pathlib import Path

# 让测试在未安装包的情况下直接跑源码目录
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from memvault.db import Database
from memvault.embeddings import FakeTextEmbedder
from memvault.memory import Memory
from memvault.vector_store import VectorStore


@pytest.fixture()
def memory(tmp_path):
    db = Database(tmp_path / "test.db")
    vs = VectorStore(tmp_path / "chroma")
    return Memory(db, vs, FakeTextEmbedder())
