"""发送工的程序侧结果；未执行证据只能由发送前校验提供。"""
from dataclasses import dataclass


@dataclass(frozen=True)
class DeliveryResult:
    ok: bool
    ack: str = ""
    why: str = ""
    not_executed: bool = False

    def __iter__(self):
        # 兼容既有三值消费方；执行证据由队列入口单独读取。
        yield self.ok
        yield self.ack
        yield self.why
