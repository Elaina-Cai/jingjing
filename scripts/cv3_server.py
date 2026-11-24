"""CosyVoice3 常驻小服务（在 WSL 里跑，模型只加载一次）。

为什么要有它：底模加载一次要 60–80 秒，逐句起进程根本没法用。
协议：TCP 127.0.0.1:<port>，一行 JSON 进、一行 JSON 出。
  {"op": "ping"}                              -> {"ok": true, "ready": <bool>, "styles": [...]}
  {"op": "say", "text": "...", "style": "cute",
   "out": "/mnt/c/.../x.wav", "seed": 42, "speed": 1.0}
                                              -> {"ok": true, "out": ..., "seconds": 12.3, "peak": 0.6}
  {"op": "shutdown"}                          -> {"ok": true}

风格：
  cute   用飞鱼 F6 微调权重 load_state_dict 覆盖底模的 llm（官方 infer.py 的做法：装进 fp32 参数里完成上转）
  gentle 直接用底模 llm，不覆盖

注意：底模目录里的 llm.pt 必须是**官方 fp32 底模**，不能被微调权重替换。
     当初把 llm.pt 直接换成飞鱼权重，参数就变成 bf16，才有那句 mat1/mat2 dtype 报错。
"""
import argparse
import json
import os
import socket
import sys
import time
import traceback


def _load(args):
    sys.path.insert(0, args.repo)
    sys.path.insert(0, os.path.join(args.repo, 'third_party', 'Matcha-TTS'))
    os.environ.setdefault('HF_HUB_OFFLINE', '1')
    os.environ.setdefault('TRANSFORMERS_OFFLINE', '1')
    os.environ.setdefault('PYTHONDONTWRITEBYTECODE', '1')

    import torch
    from cosyvoice.cli.cosyvoice import AutoModel
    from cosyvoice.utils.common import set_all_random_seed

    base = os.path.join(args.pack, 'models', 'Fun-CosyVoice3-0.5B-2512')
    torch.set_num_threads(4)
    t0 = time.time()
    if not os.path.isdir(base):
        print('PACK NOT FOUND: %s' % base, flush=True)
        sys.exit(3)
    print('BASE %s' % base, flush=True)
    voice = AutoModel(model_dir=base, fp16=False, load_trt=False, load_vllm=False)
    print('LOADED %.1fs dtype=%s' % (time.time() - t0, next(voice.model.llm.parameters()).dtype), flush=True)
    # 2026-10-26：llm.pt（含飞鱼 F6）参数是 bfloat16，但 CosyVoice3LM 里后建的
    # embedding 默认 float32 —— 相加后 inputs_embeds 是 float32，撞 q_proj 的
    # bf16 权重，报 mat1/mat2 dtype，LLM 直接吐 0 token（下游 conv 报错是假象）。
    # 统一到权重本身的 dtype。
    try:
        _w = next(voice.model.llm.parameters())
        _dtypes = {n: str(p.dtype) for n, p in voice.model.llm.named_parameters() if 'embed' in n}
        print('DTYPES %s %s' % (_w.dtype, json.dumps(_dtypes)), flush=True)
        voice.model.llm = voice.model.llm.to(_w.dtype)
        print('CAST llm -> %s' % _w.dtype, flush=True)
    except Exception as exc:  # 不让 dtype 收尾挡住服务
        print('CAST FAILED %s' % exc, flush=True)
    voice.model.llm.eval()
    set_all_random_seed(42)
    return voice


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=9881)
    ap.add_argument('--pack', required=True, help='飞鱼包目录（含 models/ 与 references/）')
    ap.add_argument('--repo', default=os.path.expanduser('~/CosyVoice'))
    ap.add_argument('--warm', default='', help='启动后先合成一句暖机')
    args = ap.parse_args()

    voice = _load(args)
    import torch
    import soundfile as sf
    import numpy as np
    from cosyvoice.utils.common import set_all_random_seed

    base = os.path.join(args.pack, 'models', 'Fun-CosyVoice3-0.5B-2512')
    styles = {
        'cute': {
            'checkpoint': os.path.join(args.pack, 'models', 'feiyu_cute_F6_0040.pt'),
            'reference': os.path.join(args.pack, 'references', 'cute_N01.wav'),
            'text': '我已经准备好了，我们开始吧。',
        },
        'gentle': {
            'checkpoint': '',
            'reference': os.path.join(args.pack, 'references', 'gentle_N10.wav'),
            'text': '我刚刚把桌子收拾了一下，也给自己倒了一杯温水。现在可以坐下来，安安静静地听你说话了。',
        },
    }
    prefix = 'You are a helpful assistant.<|endofprompt|>'
    applied = {'style': 'gentle'}
    cache = {}

    def apply_style(style):
        spec = styles[style]
        ckpt = spec['checkpoint']
        if style == applied['style']:
            return
        if style == 'cute':
            if 'cute' not in cache:
                print('LOAD_STATE cute', flush=True)
                cache['cute'] = torch.load(ckpt, map_location='cpu', weights_only=True)
            voice.model.llm.load_state_dict(cache['cute'], strict=True)
        else:
            if 'base' not in cache:
                print('LOAD_STATE base', flush=True)
                cache['base'] = torch.load(os.path.join(base, 'llm.pt'), map_location='cpu', weights_only=True)
            voice.model.llm.load_state_dict(cache['base'], strict=True)
        voice.model.llm.eval()
        applied['style'] = style
        print('STYLE %s' % style, flush=True)

    def say(req):
        style = str(req.get('style') or 'cute')
        if style not in styles:
            return {'ok': False, 'error': 'unknown style %s' % style}
        text = str(req.get('text') or '').strip()
        if not text:
            return {'ok': False, 'error': 'empty text'}
        out = str(req.get('out') or '')
        if not out:
            return {'ok': False, 'error': 'no out path'}
        seed = int(req.get('seed') or 42)
        speed = float(req.get('speed') or 1.0)
        apply_style(style)
        set_all_random_seed(seed)
        spec = styles[style]
        t0 = time.time()
        chunks = []
        with torch.inference_mode():
            for piece in voice.inference_zero_shot(text, prefix + spec['text'], spec['reference'],
                                                  stream=False, speed=speed, text_frontend=True):
                chunks.append(piece['tts_speech'].detach().cpu().numpy()[0])
        if not chunks:
            return {'ok': False, 'error': 'no audio'}
        audio = np.concatenate(chunks)
        peak = float(np.abs(audio).max())
        os.makedirs(os.path.dirname(out), exist_ok=True)
        sf.write(out, audio, voice.sample_rate, subtype='PCM_16')
        return {'ok': True, 'out': out, 'seconds': round(len(audio) / voice.sample_rate, 3),
                'peak': round(peak, 4), 'style': style, 'sample_rate': voice.sample_rate,
                'runtime_s': round(time.time() - t0, 1)}

    if args.warm:
        print('WARM %s' % json.dumps(say({'text': args.warm, 'style': 'cute',
                                          'out': '/tmp/cv3_warm.wav'}), ensure_ascii=False), flush=True)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('127.0.0.1', args.port))
    srv.listen(8)
    print('READY port=%d pid=%d' % (args.port, os.getpid()), flush=True)

    while True:
        conn, _ = srv.accept()
        try:
            fh = conn.makefile('rwb')
            line = fh.readline()
            if not line:
                continue
            req = json.loads(line.decode('utf-8'))
            op = str(req.get('op') or 'ping')
            if op == 'ping':
                reply = {'ok': True, 'ready': True, 'styles': list(styles), 'applied': applied['style']}
            elif op == 'shutdown':
                fh.write((json.dumps({'ok': True}) + '\n').encode('utf-8'))
                fh.flush()
                conn.close()
                print('SHUTDOWN', flush=True)
                return
            elif op == 'say':
                try:
                    reply = say(req)
                except Exception as exc:
                    reply = {'ok': False, 'error': '%s: %s' % (type(exc).__name__, str(exc)[:300])}
                    traceback.print_exc()
            else:
                reply = {'ok': False, 'error': 'unknown op %s' % op}
            fh.write((json.dumps(reply, ensure_ascii=False) + '\n').encode('utf-8'))
            fh.flush()
        except Exception:
            traceback.print_exc()
        finally:
            try:
                conn.close()
            except Exception:
                pass


if __name__ == '__main__':
    main()
