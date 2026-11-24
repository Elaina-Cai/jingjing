# -*- coding: utf-8 -*-
"""把 docs/decisions 的正文嵌进 output/3d/viewer_css.html，点板/点立方体层时右侧显示全文。"""
import html as H
import json
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VIEWER = ROOT / "output/3d/viewer_css.html"
DOCS = ROOT / "docs/decisions"

SET = {
    "判投记":   ["判投记.md"],
    "留痕":     ["判投记_契约.md"],
    "账本":     ["账本·事件流.md"],
    "出口":     ["出口.md"],
    "适配器":   ["软件适配器.md"],
    "感知层":   ["感知层.md"],
    "世界窗口": ["世界窗口.md", "窗口_渲染规格.md"],
    "工具包":   ["工具包.md"],
    "在手的事": ["办_在手的事.md"],
    "记忆":     ["记忆立方体.md"],
    "人设":     ["人设层_存储与读取.md"],
    "态度":     ["态度层_存储与读取.md"],
    "情绪":     ["情绪层_存储与读取.md"],
}
TITLE = {"判投记":"判 / 投 / 记","留痕":"每回合留痕","账本":"账本 · 事件流","出口":"出口",
         "适配器":"软件 · 适配器","感知层":"感知层 ①","世界窗口":"世界窗口","工具包":"工具包","在手的事":"在手的事",
         "记忆":"记忆立方体","人设":"自我立方体 ① 人设","态度":"自我立方体 ② 态度","情绪":"自我立方体 ③ 情绪"}


def inline(s: str) -> str:
    s = H.escape(s, quote=False)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"~~([^~]+)~~", r"<del>\1</del>", s)
    s = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r'<a href="\2" target="_blank">\1</a>', s)
    return s


def md2html(text: str) -> str:
    lines = text.replace("\r\n", "\n").split("\n")
    out, i, n = [], 0, len(lines)
    while i < n:
        ln = lines[i]
        if ln.startswith("```"):
            i += 1
            buf = []
            while i < n and not lines[i].startswith("```"):
                buf.append(lines[i]); i += 1
            i += 1
            out.append("<pre>" + H.escape("\n".join(buf)) + "</pre>")
            continue
        if ln.startswith("|"):
            rows = []
            while i < n and lines[i].startswith("|"):
                rows.append(lines[i]); i += 1
            cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
            body = []
            for k, r in enumerate(cells):
                if k == 1 and r and all(re.fullmatch(r":?-{2,}:?", c or "---") for c in r):
                    continue
                body.append(r)
            if body:
                if len(body) >= 2:
                    head, rest = body[0], body[1:]
                    out.append("<table><thead><tr>" + "".join(f"<th>{inline(c)}</th>" for c in head) +
                               "</tr></thead><tbody>" +
                               "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>" for r in rest) +
                               "</tbody></table>")
                else:
                    out.append("<table><tbody>" + "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>" for r in body) + "</tbody></table>")
            continue
        m = re.match(r"^(#{1,4})\s+(.*)$", ln)
        if m:
            lv = len(m.group(1))
            out.append(f"<h{lv}>{inline(m.group(2))}</h{lv}>")
            i += 1
            continue
        if re.match(r"^\s*[-*]\s+", ln):
            items, base = [], None
            while i < n:
                cur = lines[i]
                if re.match(r"^\s*[-*]\s+", cur):
                    mm = re.match(r"^(\s*)[-*]\s+(.*)$", cur)
                    ind = len(mm.group(1))
                    base = ind if base is None else base
                    items.append((ind - base, mm.group(2)))
                    i += 1
                elif not cur.strip() and i + 1 < n and re.match(r"^\s*[-*]\s+", lines[i + 1]):
                    i += 1
                else:
                    break
            buf, stack = ["<ul>"], 0
            for depth, txt in items:
                lv = depth // 2
                if lv > stack:
                    buf.append("<ul>" * (lv - stack)); stack = lv
                elif lv < stack:
                    buf.append("</ul>" * (stack - lv)); stack = lv
                buf.append(f"<li>{inline(txt)}</li>")
            buf.append("</ul>" * (stack + 1))
            out.append("".join(buf))
            continue
        if re.match(r"^>\s?", ln):
            buf = []
            while i < n and re.match(r"^>\s?", lines[i]):
                buf.append(re.sub(r"^>\s?", "", lines[i])); i += 1
            out.append("<blockquote>" + "<br>".join(inline(x) for x in buf) + "</blockquote>")
            continue
        if re.fullmatch(r"(?:-{3,}|\*{3,})", ln.strip()):
            out.append("<hr>"); i += 1; continue
        if not ln.strip():
            i += 1; continue
        buf = [ln]; i += 1
        while i < n and lines[i].strip() and not re.match(r"^(#{1,4}\s|```|\||>\s?|\s*[-*]\s+)", lines[i]):
            buf.append(lines[i]); i += 1
        out.append("<p>" + "<br>".join(inline(x) for x in buf) + "</p>")
    return "\n".join(out)


payload = {}
for key, files in SET.items():
    parts = []
    for f in files:
        p = DOCS / f
        if not p.exists():
            print("warn: missing", p); continue
        parts.append(md2html(p.read_text(encoding="utf-8")))
    payload[key] = (
        '<div class="dhead">' + H.escape(TITLE[key]) + ' <span class="dpath">' +
        H.escape(" / ".join(files)) + '</span></div>' + "\n" + "\n".join(parts)
    )

src = VIEWER.read_text(encoding="utf-8")
shutil.copy2(VIEWER, VIEWER.with_name("viewer_css.html.bak_before_docpanel"))

CSS = """
/* === DOCPANEL CSS === */
#doc{position:fixed;right:0;top:0;height:100vh;width:var(--docw);z-index:29;display:none;
 background:rgba(7,10,15,.975);border-left:1px solid #1d2a3a;box-shadow:-24px 0 48px rgba(0,0,0,.55);
 color:#cfdcea;font:12.5px/1.58 Consolas,"Microsoft YaHei",monospace;overflow:auto;padding:0 20px 60px}
#doc.on{display:block}
#doc .dhead{position:sticky;top:0;background:rgba(7,10,15,.99);border-bottom:1px solid #1d2a3a;
 padding:10px 0 8px;margin-bottom:8px;color:#9ad7ff;font-weight:700;font-size:14px;z-index:2}
#doc .dpath{color:#5d738c;font-weight:400;font-size:11px;margin-left:8px}
#doc h1{font-size:16px;color:#9ad7ff;margin:12px 0 6px}
#doc h2{font-size:14px;color:#9ad7ff;margin:16px 0 5px;border-bottom:1px dotted #1d2a3a;padding-bottom:3px}
#doc h3,#doc h4{font-size:12.5px;color:#8fc7ea;margin:12px 0 3px}
#doc pre{background:#0a1018;border:1px solid #17222f;padding:9px 10px;overflow:auto;white-space:pre;
 font-size:11.5px;line-height:1.35;color:#b9cfe3;margin:6px 0}
#doc table{border-collapse:collapse;margin:7px 0;font-size:11.5px}
#doc th,#doc td{border:1px solid #1f2c3a;padding:3px 6px;vertical-align:top}
#doc th{background:#0f1a25;color:#9ad7ff;text-align:left}
#doc ul{padding-left:17px;margin:5px 0} #doc li{margin:1px 0}
#doc code{background:#101a26;padding:0 3px;border-radius:3px;color:#a9d3f0}
#doc blockquote{border-left:2px solid #24384d;margin:6px 0;padding:2px 0 2px 10px;color:#a9bccd}
#doc hr{border:0;border-top:1px solid #17222f;margin:10px 0}
#doc a{color:#9ad7ff}
#focus.doc-open{right:var(--docw)}
#fbar.doc-open{left:calc((100vw - var(--docw)) / 2)}

:root{--docw:min(46vw,780px)}
#docgrip{display:none;position:fixed;right:var(--docw);top:0;height:100vh;width:10px;z-index:31;
 margin-right:-5px;cursor:col-resize;background:transparent}
#docgrip:hover,#docgrip.on{background:linear-gradient(90deg,rgba(154,215,255,0),rgba(154,215,255,.35))}
#docgrip::after{content:"";position:absolute;left:4px;top:0;height:100%;width:1px;background:#1d2a3a}
/* === /DOCPANEL CSS === */
"""

BODY = """
<!-- DOCPANEL -->
<div id="docgrip" title="拖动可拉伸 · 双击复位"></div>
<div id="doc"><div id="docbody"></div></div>
<script id="docdata" type="application/json">__DOCJSON__</script>
<!-- /DOCPANEL -->
"""

JS = """
/* === DOCPANEL JS === */
function _dec(s){ try { return decodeURIComponent(s); } catch(e){ return s; } }
const DALIAS = {mood:'情绪',att:'态度',persona:'人设',mem:'记忆',ledger:'账本',perceive:'感知层',toolbox:'工具包',exit:'出口',soft:'适配器',judge:'判投记',window:'世界窗口',trace:'留痕'};
const FLABEL = {perceive:'感知层',ledger:'账本 · 事件流',toolbox:'工具包',exit:'出口',soft:'软件 · 适配器',judge:'判/投/记',trace:'留痕条',window:'世界窗口'};
const DOCHTML = JSON.parse(document.getElementById('docdata').textContent);
const DOCKEY = {'判/投/记':'判投记','留痕条':'留痕','账本 · 事件流':'账本','出口':'出口','在手的事':'在手的事',
  '软件 · 适配器':'适配器','感知层':'感知层','世界窗口':'世界窗口','工具包':'工具包'};
const CUBEKEY = {self:['人设','态度','情绪'], mem:['记忆','记忆','记忆']};
const docEl = document.getElementById('doc');
const docBody = document.getElementById('docbody');
let docKeyOpen = null;
function showDoc(k){
  k = DALIAS[k] || k;
  if (!k || !DOCHTML[k]) { hideDoc(); return; }
  if (docKeyOpen === k) return;
  docBody.innerHTML = DOCHTML[k];
  docEl.scrollTop = 0;
  docEl.classList.add('on'); focusEl.classList.add('doc-open'); fbar.classList.add('doc-open');
  var _g=document.getElementById('docgrip'); if(_g) _g.style.display='block';
  docKeyOpen = k;
}
function hideDoc(){
  if (!docKeyOpen) return;
  docEl.classList.remove('on'); focusEl.classList.remove('doc-open'); fbar.classList.remove('doc-open');
  var _g=document.getElementById('docgrip'); if(_g) _g.style.display='none';
  docKeyOpen = null;
}
function docForKey(){
  if (focused) return DOCKEY[focused.label] || null;
  const c = CUBES[active];
  if (c && c.sel >= 0) return CUBEKEY[c.key][ORDER[c.sel]] || null;
  return null;
}

/* --- DOCGRIP：面板左缘可拖动拉伸 --- */
const DOCW_DEFAULT = Math.min(innerWidth * 0.46, 780);
function setDocW(px, save){
  px = Math.max(320, Math.min(innerWidth * 0.94, Math.round(px)));
  document.documentElement.style.setProperty('--docw', px + 'px');
  if (save !== false) { try { localStorage.setItem('docw', String(px)); } catch(e){} }
  return px;
}
try { const _s = localStorage.getItem('docw'); if (_s) setDocW(parseFloat(_s), false); } catch(e){}
(function(){
  const g = document.getElementById('docgrip');
  if (!g) return;
  let drag = false;
  g.addEventListener('pointerdown', function(e){
    drag = true; g.classList.add('on');
    try { g.setPointerCapture(e.pointerId); } catch(err){}
    e.preventDefault(); e.stopPropagation();
  });
  g.addEventListener('pointermove', function(e){
    if (!drag) return;
    setDocW(innerWidth - e.clientX);
  });
  function end(e){ drag = false; g.classList.remove('on');
    try { g.releasePointerCapture(e.pointerId); } catch(err){} }
  g.addEventListener('pointerup', end);
  g.addEventListener('pointercancel', end);
  g.addEventListener('dblclick', function(e){
    e.preventDefault(); e.stopPropagation(); setDocW(DOCW_DEFAULT);
  });
  addEventListener('resize', function(){ setDocW(parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--docw')) || DOCW_DEFAULT, false); });
})();
/* === /DOCPANEL JS === */
"""

def put_block(text, start, end, block, before_marker):
    pat = re.compile(re.escape(start) + r".*?" + re.escape(end), re.S)
    if pat.search(text):
        return pat.sub(lambda m: block.strip("\n"), text, count=1)
    return text.replace(before_marker, block.strip("\n") + "\n" + before_marker, 1)

src = put_block(src, "/* === DOCPANEL CSS === */", "/* === /DOCPANEL CSS === */", CSS, "</style>")
docjson = json.dumps(payload, ensure_ascii=False)
body = BODY.replace("__DOCJSON__", docjson.replace("</script>", "<\\/script>"))
src = put_block(src, "<!-- DOCPANEL -->", "<!-- /DOCPANEL -->", body, "<script>\nconst U = 56")
src = put_block(src, "/* === DOCPANEL JS === */", "/* === /DOCPANEL JS === */", JS, "// ===== 可点击聚焦")

# 聚焦时联动
src = src.replace("  nowEl.textContent = ent.label + ' · 已放大（点别的板切换，Esc 退出）';",
                  "  nowEl.textContent = ent.label + ' · 已放大（右侧是该板的全文，Esc 退出）';\n  showDoc(DOCKEY[ent.label] || null);")
src = src.replace("  focused = null;\n  focusEl.classList.remove('on');",
                  "  focused = null;\n  hideDoc();\n  focusEl.classList.remove('on');")
# 立方体层联动
src = src.replace("  const c = CUBES[active];\n  if (c.sel >= 0) {",
                  "  const c = CUBES[active];\n  if (c.sel >= 0) {\n    showDoc(CUBEKEY[c.key][ORDER[c.sel]] || null);")
src = src.replace("  } else {\n    nowEl.textContent = '点一下任一方盒/任意板 → 换板或放大；Q / E 切换立方体';",
                  "  } else {\n    hideDoc();\n    nowEl.textContent = '点一下任一方盒/任意板 → 换板或放大；Q / E 切换立方体';")
# Esc 先关文档面板
src = src.replace("  if (k === 'escape') {\n    e.preventDefault();",
                  "  if (k === 'escape') {\n    e.preventDefault();\n    if (docKeyOpen) { hideDoc(); return; }")
# D 键开关面板
src = src.replace("  if (k === 'h') {",
                  "  if (k === 'd') { e.preventDefault(); if (docKeyOpen) hideDoc(); else showDoc(docForKey()); return; }\n  if (k === 'h') {")
# hash: doc=xxx
src = src.replace("  if (kv.focus) {",
                  "  if (kv.docw && !isNaN(kv.docw)) { setDocW(parseFloat(kv.docw)); }\n  if (kv.doc && kv.doc !== 'off') { setTimeout(function(){ showDoc(kv.doc); }, 0); }\n  if (kv.focus) {")

# DOCPANEL HASHFIX: hash 里的中文会被百分号编码
src = src.replace('kv[p.slice(0,i).trim()] = p.slice(i+1).trim();',
                  'kv[p.slice(0,i).trim()] = _dec(p.slice(i+1).trim());')
src = src.replace('if(i>0)kv[p.slice(0,i).trim()]=p.slice(i+1).trim();',
                  'if(i>0)kv[p.slice(0,i).trim()]=_dec(p.slice(i+1).trim());')
src = src.replace('const e = ENT.find(function(x){ return x.label === kv.focus || x.tex === kv.focus; });',
                  'const fk = FLABEL[kv.focus] || kv.focus;\n    const e = ENT.find(function(x){ return x.label === fk || x.tex === fk; });')
VIEWER.write_text(src, encoding="utf-8", newline="\n")
print("viewer:", len(src.encode()), "B,", len(src.splitlines()), "行")
print("doc keys:", len(payload), "total html:", sum(len(v) for v in payload.values()), "chars")