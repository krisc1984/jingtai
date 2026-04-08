#!/usr/bin/env python3
import mammoth
import sys

input_file = "/mnt/c/Users/xiaoxuan/Downloads/🦞 胖福虾的新西兰皇后镇之旅.docx"
output_file = "/mnt/c/Users/xiaoxuan/Downloads/🦞 胖福虾的新西兰皇后镇之旅.html"

with open(input_file, "rb") as docx_file:
    result = mammoth.convert_to_html(docx_file)
    html = result.value
    messages = result.messages
    
    # 添加完整的 HTML 结构
    full_html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>胖福虾的新西兰皇后镇之旅</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            line-height: 1.6;
            max-width: 800px;
            margin: 0 auto;
            padding: 20px;
            color: #333;
        }}
        h1, h2, h3, h4, h5, h6 {{
            color: #1a1a2e;
            margin-top: 1.5em;
            margin-bottom: 0.5em;
        }}
        h1 {{ font-size: 2em; border-bottom: 2px solid #eaeaea; padding-bottom: 0.3em; }}
        h2 {{ font-size: 1.5em; border-bottom: 1px solid #eaeaea; padding-bottom: 0.3em; }}
        p {{ margin-bottom: 1em; }}
        img {{ max-width: 100%; height: auto; display: block; margin: 1em auto; }}
        table {{ border-collapse: collapse; width: 100%; margin: 1em 0; }}
        th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
        th {{ background-color: #f5f5f5; font-weight: bold; }}
        ul, ol {{ margin-bottom: 1em; padding-left: 2em; }}
        li {{ margin-bottom: 0.5em; }}
        blockquote {{
            border-left: 4px solid #ddd;
            margin: 1em 0;
            padding-left: 1em;
            color: #666;
        }}
    </style>
</head>
<body>
{html}
</body>
</html>"""
    
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(full_html)
    
    print(f"✅ 转换成功：{output_file}")
    if messages:
        print(f"⚠️ 转换过程中有 {len(messages)} 条消息:")
        for msg in messages[:10]:
            print(f"  - {msg}")
