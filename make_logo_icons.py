"""一次性脚本：从 assets/logo-icon-512.jpg 生成 favicon/apple-touch-icon/站点 logo
各尺寸 PNG 的 base64 常量文本，输出到 assets/_logo_b64.txt，供粘贴进 app.py。
同时落地 PNG 文件到 assets/ 供核对。"""
import base64
import io
import os
from PIL import Image

src = Image.open("assets/logo-icon-512.jpg").convert("RGB")


def gen(size):
    img = src.resize((size, size), Image.LANCZOS) if size != src.width else src
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


out_lines = []
for name, size in [("FAVICON_PNG32", 32), ("APPLE_TOUCH_PNG180", 180), ("LOGO_PNG192", 192)]:
    raw = gen(size)
    with open(os.path.join("assets", f"{name.lower()}.png"), "wb") as f:
        f.write(raw)
    s = base64.b64encode(raw).decode()
    out_lines.append(f"# {size}x{size} PNG, {len(raw)} bytes")
    out_lines.append(f"_{name}_B64 = (")
    for i in range(0, len(s), 56):
        out_lines.append(f'    "{s[i:i+56]}"')
    out_lines.append(")")
    out_lines.append("")

with open("assets/_logo_b64.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(out_lines))
print("\n".join(l for l in out_lines if l.startswith("#")))
print("written assets/_logo_b64.txt")
