from __future__ import annotations

from db.models import init_db
from db.repo_base import BaseRepository
from db.write_guard import WriteHandle
from db.repo_messages import MessageRepository
from db.repo_contacts import ContactRepository
from db.repo_analysis import AnalysisRepository, SyncStateRepository, UserProfileRepository
from db.repo_persona import PersonaRepository
from db.repo_memory import ConversationRepository, SessionRepository, CoreMemoryRepository
from db.repo_reminder import ReminderRepository, RelationshipRepository
from db.repo_threads import TopicRepository, ThreadRepository, ThreadMsgRefRepository
from db.repo_knowledge import TodoRepository, KnowledgeDocRepository
from db.repo_social import FrictionEventRepository
from db.repo_sendlog import SendLogRepository
from db.repo_actionlog import ActionLogRepository


class RepositoryManager:
    """单库时代的仓库集合（只读读法见 ``db.write_guard.read_only_engine``）。

    **构造函数不过守卫**（构造 ≠ 要写）：``init_db`` 会 ``create_all`` + 跑迁移，那一步
    才是写，所以守卫挪到"**真的拿可写句柄**"那两处（:attr:`engine` /
    :meth:`write_engine`）——默认拒绝未声明进程对生产路径建可写句柄，隔离路径照常。
    非生产路径在构造末尾由 ``WriteHandle.schema_ready()`` 当场建好（"构造完就是可用的库"
    这条既有语义不变）；生产路径不建，构造不碰盘。判据来源只有
    ``core.bootstrap.production_data_paths`` 一处。
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        #: 可写句柄（**惰性 + 写时断言**）：构造到这里为止一个字节都不碰盘。
        self._write = WriteHandle(
            db_path, what="仓库的可写 engine",
            factory=lambda: init_db(db_path),
        )
        self.messages = MessageRepository(self._write)
        self.contacts = ContactRepository(self._write)
        self.analysis = AnalysisRepository(self._write)
        self.sync_state = SyncStateRepository(self._write)
        self.user_profile = UserProfileRepository(self._write)
        self.personas = PersonaRepository(self._write)
        self.conversations = ConversationRepository(self._write)
        self.sessions = SessionRepository(self._write)
        self.core_memory = CoreMemoryRepository(self._write)
        self.reminders = ReminderRepository(self._write)
        self.relationships = RelationshipRepository(self._write)
        self.topics = TopicRepository(self._write)
        self.threads = ThreadRepository(self._write)
        self.thread_refs = ThreadMsgRefRepository(self._write)
        self.friction = FrictionEventRepository(self._write)
        self.todos = TodoRepository(self._write)
        self.knowledge_docs = KnowledgeDocRepository(self._write)
        self.send_log = SendLogRepository(self._write)
        self.action_log = ActionLogRepository(self._write)
        # 非生产路径当场建好（构造完就是可用的库，既有语义逐字不变）；生产路径不建，
        # 留给第一次真用时的守卫（**构造 ≠ 要写**）。见 WriteHandle.schema_ready。
        self._write.schema_ready()

    def write_engine(self):
        """**拿可写 engine**——守卫就在这一步（生产路径 + 未声明 ⇒ ``ProductionWriteRefused``）。"""
        return self._write.handle()

    @property
    def engine(self):
        """**真** engine——拿它就是要写，所以过守卫（见 :meth:`write_engine`）。"""
        return self._write.handle()


__all__ = [
    "BaseRepository", "RepositoryManager",
    "MessageRepository", "ContactRepository",
    "AnalysisRepository", "SyncStateRepository", "UserProfileRepository",
    "PersonaRepository",
    "ConversationRepository", "SessionRepository", "CoreMemoryRepository",
    "ReminderRepository", "RelationshipRepository",
    "TopicRepository", "ThreadRepository", "ThreadMsgRefRepository",
    "TodoRepository", "KnowledgeDocRepository",
    "FrictionEventRepository",
    "SendLogRepository", "ActionLogRepository",
]
