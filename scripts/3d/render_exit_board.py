# -*- coding: utf-8 -*-
from PIL import Image, ImageDraw, ImageFont
from pathlib import Path
BG=(10,14,22); BORDER=(120,200,255); TITLE=(168,226,255); TEXT=(228,234,242); DIM=(146,158,176)
F=lambda n,s: ImageFont.truetype(rf"C:\Windows\Fonts\{n}", s)

def board(w,h,title,lines,out,tsize=68,lsize=30):
    img=Image.new("RGB",(w,h),BG); d=ImageDraw.Draw(img)
    d.rounded_rectangle([6,6,w-6,h-6],radius=16,outline=BORDER,width=3)
    d.text((w/2,h*0.30),title,font=F("msyhbd.ttc",tsize),fill=TITLE,anchor="mm")
    y=h*0.56
    for t,c in lines:
        d.text((w/2,y),t,font=F("msyh.ttc",lsize),fill=c,anchor="mm"); y+=lsize*1.5
    img.save(out); print("wrote",out,img.size,Path(out).stat().st_size,"bytes")

board(600,440,"出  口",[("只放行 · 不判",TEXT),("按注册表，不限平台",DIM)],"output/3d/t_exit.png")

board(760,480,"软 件 · 适配器",[("聊天 · 日历 · 邮件 · 浏览器",TEXT),
                            ("内部系统 · 任何注册过的软件",TEXT),
                            ("一次投递 = 调一个 adapter",DIM)],"output/3d/t_soft.png",tsize=58,lsize=28)