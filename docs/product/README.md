# 产品介绍页

`calendar-memo-product.html` — 日历备忘录的产品介绍页源文件。

## 线上地址

<https://xmtooldev.gf.com.cn/calendar-memo/>

## 说明

- 这里的 HTML 是**源文件**，下载链接写的是包内相对路径（`日历备忘录-*.exe` / `.zip`）。
- 直接双击打开时，同目录没有这些安装包，**下载按钮会失效**；这是预期的 ——
  真正可下载的版本在 xmtooldev 上，发布时安装包被复制进 `assets/` 并改成 ASCII 名
  （`calendar-memo-setup-1.0.0.exe` 等）。
- 发布流程见 `docs/PACKAGING.md` 与 skill `xmtooldev-publish`；
  不要在仓库里提交安装包二进制，它们体积 25–31 MB 且属于构建产物。

## 修改后如何重新发布

```bash
# 1. 准备（改写下载链接到 assets/、注入返回入口）
python ~/.workbuddy/skills/xmtooldev-publish/scripts/prepare_publish_dir.py \
  calendar-memo docs/product/calendar-memo-product.html <输出目录> \
  "dist/日历备忘录-1.0.0-安装包.exe=calendar-memo-setup-1.0.0.exe" \
  "dist/日历备忘录-Windows-x64.zip=calendar-memo-portable-x64.zip" \
  "dist/日历备忘录-Tauri版-Windows-x64.zip=calendar-memo-tauri-x64.zip"

# 2. 发布（自动快照 + 原子落盘 + 冒烟 + 失败回滚）
python ~/.workbuddy/skills/xmtooldev-publish/scripts/publish_to_xmtooldev.py \
  calendar-memo <输出目录>
```

首页卡片（`t-idx` / `g-cnt`）需手工维护，见 skill 步骤 3。
