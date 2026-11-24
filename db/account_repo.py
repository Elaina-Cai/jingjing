from __future__ import annotations

from db.account_models import init_account_db
from db.repo_base import BaseRepository
from db.repo_messages import MessageRepository
from db.repo_contacts import ContactRepository
from db.repo_analysis import AnalysisRepository, SyncStateRepository, UserProfileRepository
from db.repo_sendlog import SendLogRepository
from db.repo_actionlog import ActionLogRepository


class AccountRepositoryManager:
    def __init__(self, db_path: str, platform: str, account_id: str):
        self.db_path = db_path
        self.platform = platform
        self.account_id = account_id
        self.engine = init_account_db(db_path)
        self.messages = MessageRepository(self.engine)
        self.contacts = ContactRepository(self.engine)
        self.analysis = AnalysisRepository(self.engine)
        self.sync_state = SyncStateRepository(self.engine)
        self.user_profile = UserProfileRepository(self.engine)
        self.send_log = SendLogRepository(self.engine)
        self.action_log = ActionLogRepository(self.engine)
