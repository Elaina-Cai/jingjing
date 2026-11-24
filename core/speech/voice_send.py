# -*- coding: utf-8 -*-
# 语音条发送：先老实说清楚每个平台能不能发，再谈怎么发
#
# 2026-10-26 实测结论：
#   微信 PC —— 没有发语音的接口。界面上只有一个「按住说话」按钮，语音是实时录音
#              上传的；把 silk 文件拖进窗口只会变成文件消息。GUI 自动化要发语音，
#              只能「压住说话键 + 把音频播进虚拟声卡当麦克风」，依赖第三方驱动
#              （VB-Cable 之类），不是纯代码能保证的事。
#   QQ      —— napcat（OneBot）支持 record 段，是唯一能真发语音的通道；本项目
#              还没接这条线。
# 所以这个模块现在的职责是：给出能力表、把准备好的语音条交给能发的通道，
# 发不了就返回原因，绝不假装发出去了。
from __future__ import annotations

CAPABILITIES = {
    'wechat': {
        'supported': False,
        'reason': 'wechat_no_voice_api',
        'detail': '微信 PC 没有发语音的接口，只能按住说话（实时录音上传）；'
                  'silk 文件拖进窗口只会变成文件消息，不是语音条。',
        'workaround': '虚拟声卡（VB-Cable 等）+ 压住说话键回放；需要额外驱动，未实现。',
    },
    'qq': {
        'supported': False,
        'reason': 'qq_not_wired',
        'detail': 'napcat（OneBot）支持 record 段，本项目尚未接线。',
        'workaround': '走 napcat 的 send_group_msg/send_private_msg，record 字段传 base64 silk。',
    },
}


def voice_capability(platform: str) -> dict:
    key = str(platform or '').strip().lower()
    info = CAPABILITIES.get(key)
    if info is None:
        return {'platform': key, 'supported': False, 'reason': 'unknown_platform',
                'detail': '未知平台：%s' % (key or '(空)'), 'workaround': ''}
    out = dict(info)
    out['platform'] = key
    return out


def send_voice(*, platform: str, target: str, silk_path: str, text: str = '') -> dict:
    cap = voice_capability(platform)
    out = {'ok': False, 'platform': cap['platform'], 'target': str(target),
           'silk': str(silk_path), 'reason': cap.get('reason') or 'unsupported',
           'detail': cap.get('detail') or '', 'sent': False}
    if not cap.get('supported'):
        return out
    out['reason'] = 'adapter_missing'
    out['detail'] = '平台声明支持，但没有对应适配器实现。'
    return out
