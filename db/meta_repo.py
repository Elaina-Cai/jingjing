from __future__ import annotations

from db.meta_models import init_meta_db
from db.repo_base import BaseRepository
from db.write_guard import WriteHandle
from db.repo_persona import PersonaRepository
from db.repo_memory import ConversationRepository, SessionRepository, CoreMemoryRepository
from db.repo_reminder import ReminderRepository, RelationshipRepository
from db.repo_threads import TopicRepository, ThreadRepository, ThreadMsgRefRepository
from db.repo_knowledge import TodoRepository, KnowledgeDocRepository
from db.repo_social import FrictionEventRepository
from db.repo_account_registry import AccountRegistryRepository
from db.repo_people import PeopleRepository
from db.repo_evidence import EvidenceRepository
from db.repo_evidence_cursor import EvidenceCursorRepository
from db.repo_conversation_windows import ConversationWindowRepository
from db.repo_commands import CommandRepository
from db.repo_memory_projections import MemoryProjectionRepository
from db.repo_analysis_projections import AnalysisProjectionRepository
from db.repo_feedback import FeedbackRepository
from db.repo_links import LinkGraphRepository
from db.repo_proactive import ProactiveTargetRepository
from db.repo_state import StateRepository


class MetaRepositoryManager:
    """meta 库的仓库集合。

    守卫的位置（本类最要紧的一件事）
    --------------------------------
    **构造函数不过守卫**：``db/write_guard.py`` 自己写着"读也堵死 = 全仓测试与只读脚本
    全崩"。构造一个仓库**不等于**这个调用方要写——曾经把 ``assert_writes_allowed`` 放在
    ``__init__`` 里（因为 ``init_meta_db`` 会 ``create_all`` + 跑迁移，看起来"构造 =
    建可写句柄"），结果是**只想读的调用方连 new 都 new 不出来**，那 4 个直接跑用例与
    只读脚本一起红。守卫因此挪到"**真的拿可写句柄**"那两处：:attr:`engine` /
    :meth:`write_engine`（默认拒绝未声明的进程对**生产路径**建可写句柄——2026-10-02
    22:5x 第三次事故：写口回落到 ``core.bootstrap.get_meta_repo()``，测试数据落进生产
    ``claim_ledger``）。判据只有一处来源（``core.bootstrap.production_data_paths``）。

    "推迟建 engine"为什么还要 ``schema_ready()``
    ------------------------------------------
    ``init_meta_db`` 顺带把 schema 建好/迁到最新，全仓大量调用方依赖"构造完就是可用的
    库"这条既有语义。所以非生产路径（隔离库 / 临时库）在构造末尾由
    :meth:`db.write_guard.WriteHandle.schema_ready` 当场建好——**那条路本来就不需要任何
    声明**，构造语义与从前逐字一致；生产路径则不建，构造不碰盘，第一次真用才过守卫。
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        #: 可写句柄（**惰性 + 写时断言**）：构造到这里为止一个字节都不碰盘。
        self._write = WriteHandle(
            db_path, what="meta 库的可写 engine",
            factory=lambda: init_meta_db(db_path),
        )
        self.account_registry = AccountRegistryRepository(self._write)
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
        self.people = PeopleRepository(self._write)
        self.evidence = EvidenceRepository(self._write)
        self.evidence_cursors = EvidenceCursorRepository(self._write)
        self.conversation_windows = ConversationWindowRepository(self._write)
        self.commands = CommandRepository(self._write)
        self.memory_projections = MemoryProjectionRepository(self._write)
        self.analysis_projections = AnalysisProjectionRepository(self._write)
        self.feedback_samples = FeedbackRepository(self._write)
        self.links = LinkGraphRepository(self._write)
        self.proactive = ProactiveTargetRepository(self._write)
        self.state = StateRepository(self._write)
        # 非生产路径当场建好（构造完就是可用的库，既有语义逐字不变）；生产路径不建，
        # 留给第一次真用时的守卫（**构造 ≠ 要写**）。见 WriteHandle.schema_ready。
        self._write.schema_ready()

    def write_engine(self):
        """**拿可写 engine**——守卫就在这一步（生产路径 + 未声明 ⇒ ``ProductionWriteRefused``）。

        过守卫之后才真建 engine（建库 / 建表 / 跑迁移），所以"拒绝"发生时目标库
        **一个字节都不会变**。只想读就不要调它。
        """
        return self._write.handle()

    @property
    def engine(self):
        """**真** engine（``sqlalchemy.engine.Engine``）——拿它就是要写，所以先过守卫。

        保留这个名字是因为调用方（``core/bootstrap.py`` / ``core/**`` / 测试 / 脚本）
        一直把 ``manager.engine`` 当**真 Engine** 用：``engine.connect()`` /
        ``engine.begin()`` / 直接喂给 ``Session(bind)``。所以本属性是
        "**先断言、再物化**"（``assert_writable()`` → ``materialize()``），**不是**
        返回那个惰性句柄——句柄不是 Engine（``Session.__init__`` 用
        ``isinstance(bind, Engine)`` 分流，冒充会让 Session 悄悄走错连接路径，SQLite 上
        表现为 "database is locked"），而"调用方拿到的东西不再是 Engine"正是 2026-10-03
        那次"``.engine`` 一改、生产五条 error 全崩"的形状。

        守卫语义与 :meth:`write_engine` 逐字相同：生产路径 + 未声明 ⇒
        ``ProductionWriteRefused``（且断言在物化之前，目标库零字节）；非生产路径
        （隔离库 / 临时库）照常放行，且构造末尾的 ``schema_ready()`` 已经把 engine
        建好了，这里只是把它取出来。
        """
        return self._write.handle()
