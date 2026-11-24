from __future__ import annotations

from datetime import datetime

from agents.tools.base import BaseTool, ToolResult
from safelog import warn


class CreateTodoTool(BaseTool):
    name = "create_todo"
    description = "创建待办事项。当用户提到需要做的事、任务、计划时调用。"
    parameters = {
        "title": {"type": "string", "description": "待办标题", "required": True},
        "description": {"type": "string", "description": "待办详细描述"},
        "priority": {"type": "integer", "description": "优先级 0-3，0最低3最高，默认1"},
        "due_at": {"type": "string", "description": "截止时间，格式 YYYY-MM-DD HH:MM"},
        "tags": {"type": "string", "description": "标签，逗号分隔"},
    }

    def __init__(self, repo):
        self.repo = repo

    async def execute(self, **kwargs) -> ToolResult:
        title = kwargs.get("title", "")
        if not title:
            return ToolResult("", error="title 不能为空")

        description = kwargs.get("description", "")
        priority = min(max(int(kwargs.get("priority", 1)), 0), 3)
        due_at_str = kwargs.get("due_at", "")
        tags = kwargs.get("tags", "")

        due_at = None
        if due_at_str:
            try:
                due_at = datetime.strptime(due_at_str.strip(), "%Y-%m-%d %H:%M")
            except ValueError:
                warn("agents/tools/todo.py:36 ValueError", err=repr(__import__("sys").exc_info()[1]))
                try:
                    due_at = datetime.strptime(due_at_str.strip(), "%Y-%m-%d")
                except ValueError:
                    warn("agents/tools/todo.py:39 ValueError", err=repr(__import__("sys").exc_info()[1]))
                    return ToolResult("", error=f"时间格式错误: {due_at_str}")

        from db.models import Todo
        active = self.repo.personas.list_active()
        pid = active[0].id if active else 1
        todo = Todo(
            persona_id=pid,
            title=title,
            description=description,
            status="pending",
            priority=priority,
            due_at=due_at,
            tags=tags,
            created_at=datetime.now(),
        )
        saved = self.repo.todos.add(todo)

        prio_labels = {0: "低", 1: "普通", 2: "高", 3: "紧急"}
        due_str = f"，截止 {due_at.strftime('%m月%d日 %H:%M')}" if due_at else ""
        return ToolResult(
            f"已创建待办: [{prio_labels.get(priority, '普通')}] {title}{due_str}",
            data={"id": saved.id, "title": title, "priority": priority},
        )


class ListTodosTool(BaseTool):
    name = "list_todos"
    description = "查看待办事项列表。当用户问有什么待办、任务列表时调用。"
    parameters = {
        "status": {"type": "string", "description": "过滤状态: pending/done/all，默认pending", "enum": ["pending", "done", "all"]},
        "limit": {"type": "integer", "description": "最多返回多少条，默认20"},
    }

    def __init__(self, repo):
        self.repo = repo

    async def execute(self, **kwargs) -> ToolResult:
        status = kwargs.get("status", "pending")
        limit = kwargs.get("limit", 20)

        active = self.repo.personas.list_active()
        pid = active[0].id if active else 1

        if status == "all":
            todos = self.repo.todos.list_all(persona_id=pid, limit=limit)
        else:
            todos = self.repo.todos.list_all(persona_id=pid, status=status, limit=limit)

        if not todos:
            return ToolResult("没有待办事项")

        prio_labels = {0: "低", 1: "普通", 2: "高", 3: "紧急"}
        lines = []
        for t in todos:
            prio = prio_labels.get(t.priority, "普通")
            due = f" ⏰{t.due_at.strftime('%m/%d')}" if t.due_at else ""
            check = "✅" if t.status == "done" else "☐"
            lines.append(f"{check} [{prio}] #{t.id} {t.title}{due}")

        return ToolResult(f"待办列表:\n" + "\n".join(lines), data=[
            {"id": t.id, "title": t.title, "status": t.status, "priority": t.priority,
             "due_at": t.due_at.isoformat() if t.due_at else None}
            for t in todos
        ])


class CompleteTodoTool(BaseTool):
    name = "complete_todo"
    description = "完成一个待办事项。当用户说某个任务完成了时调用。"
    parameters = {
        "todo_id": {"type": "integer", "description": "待办ID", "required": True},
    }

    def __init__(self, repo):
        self.repo = repo

    async def execute(self, **kwargs) -> ToolResult:
        todo_id = kwargs.get("todo_id", 0)
        if not todo_id:
            return ToolResult("", error="todo_id 不能为空")

        todo = self.repo.todos.get_by_id(todo_id)
        if not todo:
            return ToolResult("", error=f"找不到待办 #{todo_id}")

        self.repo.todos.mark_done(todo_id)
        return ToolResult(f"已完成待办: #{todo_id} {todo.title}")