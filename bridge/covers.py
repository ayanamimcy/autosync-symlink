from pathlib import Path
import random
from .paths import walk


def generate(lib, target, title, subtitle="YOUR PRIVATE CINEMA", theme="teal"):
    try:
        from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps
    except ImportError:
        raise ValueError("封面功能需要 Pillow，请先运行安装脚本安装 requirements.txt")
    palettes = {"teal":((7,22,31),(10,105,100),(93,244,210)),
                "amber":((24,17,13),(131,70,35),(255,202,109)),
                "violet":((17,14,32),(73,51,136),(186,160,255))}
    if theme not in palettes:
        raise ValueError("无效封面主题")
    title, subtitle = title.strip()[:30], subtitle.strip()[:60]
    if not title:
        title = lib["name"][:30]
    a,b,accent = palettes[theme]
    canvas = Image.new("RGB",(1600,900))
    draw = ImageDraw.Draw(canvas)
    for y in range(900):
        t = y/899
        color = tuple(int(a[k]*(1-t)+b[k]*t) for k in range(3))
        draw.line((0,y,1600,y),fill=color)
    # Posters come only from already-copied local artwork, not cloud videos.
    posters = []
    output = Path(lib["output"])
    if output.is_dir():
        for p,st in walk(output):
            if p.suffix.lower() in (".jpg",".jpeg",".png",".webp") and st.st_size < 16*1024*1024 and any(w in p.stem.lower() for w in ("poster","folder","cover")):
                posters.append(p)
                if len(posters) >= 9:
                    break
    for i in range(9):
        x,y = 850+(i%3)*230, -65+(i//3)*350
        tile = Image.new("RGB",(205,310),tuple(min(255,c+(i%3)*12) for c in b))
        if posters:
            try:
                with Image.open(posters[i%len(posters)]) as art:
                    if art.width*art.height <= 32_000_000:
                        tile = ImageOps.fit(art.convert("RGB"),(205,310))
            except (OSError,ValueError,Image.DecompressionBombError):
                pass
        tile = tile.convert("RGBA").rotate(-12,resample=Image.Resampling.BICUBIC,expand=True)
        canvas.paste(tile,(x,y),tile)
    shade = Image.new("RGBA",canvas.size)
    sd = ImageDraw.Draw(shade)
    for x in range(1600):
        alpha = int(245*(1-x/1600)**1.5)
        sd.line((x,0,x,900),fill=(*a,alpha))
    canvas = Image.alpha_composite(canvas.convert("RGBA"),shade)
    d = ImageDraw.Draw(canvas)
    font_path = next((p for p in ["C:/Windows/Fonts/msyh.ttc","C:/Windows/Fonts/simhei.ttf",
                                 "/System/Library/Fonts/STHeiti Medium.ttc",
                                 "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                                 "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"] if Path(p).is_file()),None)
    def font(size):
        return ImageFont.truetype(font_path,size) if font_path else ImageFont.load_default(size=size)
    d.rounded_rectangle((96,120,350,169),radius=24,fill=(26,51,53,255),outline=(65,102,98,255),width=1)
    d.text((118,130),"MEDIA COLLECTION",font=font(19),fill=accent)
    size = 104
    while size>36 and d.textbbox((0,0),title,font=font(size))[2]>750:
        size -= 4
    d.text((90,350),title,font=font(size),fill=(245,250,248))
    d.rectangle((98,515,172,521),fill=accent)
    d.text((98,560),subtitle,font=font(23),fill=(192,210,212))
    d.text((98,775),"AUTOSYNC  /  MEDIA LIBRARY",font=font(18),fill=(154,183,190))
    target.parent.mkdir(parents=True,exist_ok=True)
    canvas.convert("RGB").save(target,format="PNG")
    return target
