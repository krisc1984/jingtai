# SpaceX 风格设计系统

> 受 SpaceX 官网启发的极简主义设计系统 —— 摄影即界面，文字即标注

[![Design System](https://img.shields.io/badge/design-system-black?style=flat&logo=figma)](DESIGN.md)
[![License](https://img.shields.io/badge/license-MIT-black?style=flat)](LICENSE)

## 📖 概述

这是一个从 [SpaceX 官网](https://spacex.com/) 提取的设计系统，捕捉其标志性的航天工业美学：

- **纯黑画布** (`#000000`) 配合全画幅太空摄影
- **D-DIN 字体** —— 工业几何风格，全部大写 + 正字间距
- **光谱白文字** (`#f0f0fa`) —— 带有微妙的蓝紫 tint
- **幽灵按钮** —— 半透明交互元素，悬浮于摄影之上
- **零阴影、零卡片、零装饰** —— 只有摄影 + 文字

## 📁 文件结构

| 文件 | 描述 |
|------|------|
| [`DESIGN.md`](DESIGN.md) | 完整设计系统文档（9 个章节） |
| [`preview.html`](preview.html) | 交互式设计令牌目录（浅色模式） |
| [`preview-dark.html`](preview-dark.html) | 交互式设计令牌目录（深色模式） |
| [`index.html`](index.html) | 项目导航页面 |

## 🚀 快速开始

### 本地预览

```bash
# 使用任意静态文件服务器
python -m http.server 8000
# 或
npx serve .
```

然后在浏览器中打开 `http://localhost:8000/preview.html`

### 图片上传接口

项目内置了一个本地文件图床接口，上传后会落盘到 `uploads/`，并返回可直接访问的图片 URL：

```bash
python responses_proxy.py --host 0.0.0.0 --port 8010
```

如果服务前面挂了公网域名或反向代理，建议同时设置：

```bash
set PUBLIC_BASE_URL=https://your-domain.example.com
```

上传接口：

```bash
curl -X POST http://127.0.0.1:8010/v1/upload-image \
  -F "file=@C:/path/to/demo.png"
```

返回示例：

```json
{
  "object": "image_upload",
  "filename": "1749700000_ab12cd34ef56.png",
  "bytes": 12345,
  "url": "http://127.0.0.1:8010/uploads/1749700000_ab12cd34ef56.png",
  "warning": "uploaded files are publicly accessible to anyone with the URL"
}
```

说明：

- 上传接口是 `POST /v1/upload-image`
- 访问地址是 `GET /uploads/<filename>`
- 默认最大上传大小是 `20 MiB`
- 可通过 `MAX_UPLOAD_BYTES` 和 `UPLOAD_DIR` 调整
- 要让外网机器能访问，监听地址需要是 `0.0.0.0`，并确保服务器防火墙/端口映射已放行

### 在 AI 项目中使用

将 `DESIGN.md` 作为参考文档提供给 AI 助手（Claude、Cursor 等），用于生成符合 SpaceX 设计语言的 UI：

```
使用 SpaceX 设计系统创建一个全画幅 hero 区域：
- 背景：100vh 摄影，深色渐变遮罩 rgba(0,0,0,0.5)
- 标题：48px D-DIN-Bold，大写，字间距 0.96px，光谱白 (#f0f0fa)
- CTA 按钮：幽灵按钮样式，rgba(240,240,250,0.1) 背景，32px 圆角
```

## 🎨 核心设计原则

### 1. 摄影即界面
每个区块都是一个电影场景，摄影占据 100% 视口。界面完全消失在图像背后。

### 2. 文字即标注
所有文字全部大写，带有正字间距 (0.96px–1.17px)，如同航天器外壳上的模板印刷。

### 3. 极简主义
- 零阴影
- 零边框（除幽灵按钮外）
- 零色彩（只有黑 + 光谱白）
- 零卡片/容器

### 4. 单交互元素
幽灵按钮是唯一的交互组件：
```css
background: rgba(240, 240, 250, 0.1);
border: 1px solid rgba(240, 240, 250, 0.35);
border-radius: 32px;
padding: 18px;
```

## 📐 颜色系统

| 名称 | 色值 | 用途 |
|------|------|------|
| Space Black | `#000000` | 页面背景 |
| Spectral White | `#f0f0fa` | 文字颜色 |
| Ghost Surface | `rgba(240, 240, 250, 0.1)` | 按钮背景 |
| Ghost Border | `rgba(240, 240, 250, 0.35)` | 按钮边框 |
| Dark Overlay | `rgba(0, 0, 0, 0.5)` | 摄影遮罩 |

## 🔤 字体系统

### 字体系列
- **Display**: `D-DIN-Bold`
- **Body/UI**: `D-DIN`, `Arial`, `Verdana`

### 层级规范

| 角色 | 字体 | 字号 | 字重 | 字间距 |
|------|------|------|------|--------|
| Display Hero | D-DIN-Bold | 48px | 700 | 0.96px |
| Nav Link Bold | D-DIN | 13px | 700 | 1.17px |
| Body | D-DIN | 16px | 400 | normal |
| Caption | D-DIN | 12px | 400 | normal |
| Micro | D-DIN | 10px | 400 | 1px |

**所有文字使用 `text-transform: uppercase`**

## 📱 响应式断点

| 名称 | 宽度 | 关键变化 |
|------|------|----------|
| Mobile | <600px | 堆叠布局，缩小字号 |
| Tablet Small | 600–960px | 调整布局 |
| Tablet | 960–1280px | 标准缩放 |
| Desktop | 1280–1350px | 完整布局 |
| Large Desktop | 1350–1500px | 扩展布局 |
| Ultra-wide | >1500px | 最大视口 |

## ⚠️ 注意事项

### ✅ 应该做的
- 使用全画幅摄影作为主要设计元素
- 所有文字大写 + 正字间距
- 仅使用 D-DIN 字体
- 保持黑 + 光谱白的双色调色板
- 使用幽灵按钮作为唯一交互元素
- 在摄影上应用深色渐变遮罩以保证文字可读性

### ❌ 不应该做的
- 不要添加卡片、面板或容器
- 不要使用阴影
- 不要引入其他颜色
- 不要使用小写或句首大写
- 不要使用负字间距
- 不要将摄影缩小为缩略图
- 不要添加装饰元素（图标、徽章、分隔线）

## 📄 许可证

MIT License

## 🔗 相关链接

- [DESIGN.md 完整文档](DESIGN.md)
- [SpaceX 官网](https://spacex.com/)
- [VoltAgent Design MD](https://github.com/VoltAgent/awesome-design-md)

---

> 这不是 SpaceX 官方设计系统。颜色、字体和间距可能与官方不完全一致，但可作为构建类似风格的起点。
