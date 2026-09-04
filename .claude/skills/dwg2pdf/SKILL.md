---
name: dwg2pdf
description: 用本机 CAD快速看图.app 将 DWG 的每个布局（有图框的）批量导出为 PDF（GUI 自动化，仅限 macOS 本机）
---

# DWG 转 PDF（CAD快速看图 GUI 自动化）

用户给出 DWG 文件路径（可多个）时，用本 skill 的脚本逐个转换：

```bash
bash .claude/skills/dwg2pdf/dwg2pdf.sh <输入.dwg> [更多.dwg ...]
# 输出: 每张 dwg 归档为同目录 <dwg文件名>.zip（内含各布局 PDF，PDF 名 <图纸名>_<布局名>.pdf）
# KEEP_PDF=1 可在归档后保留散落的 PDF；无图框的布局自动跳过
```

## 脚本行为（2026-09-02 实测打通）

1. 清理残留对话框（Escape + 点「关闭」）
2. `open -a` 打开 DWG，轮询窗口标题确认加载
3. 递归收集底部布局标签（AXRadioButton），自动过滤文件标签页（标题带前导空格）
4. 逐个布局：切换标签 → 会员菜单第18项「批量导出PDF」（键盘导航 ↓×18 + 回车）→
   读图框数（面板左上 "N/M" 文本的 M）→ **0 图框跳过** → 点「导出」→ NSSavePanel 设
   文件名 `<图纸名>_<布局名>` → 保存 → 等 PDF 落盘且大小稳定
5. 导出后循环关闭完成提示 + 批量面板（**两个「关闭」**）
6. 每张 dwg 的全部布局 PDF 用 python3 zipfile 归档为 `<dwg文件名>.zip`（系统 `zip` 命令
   不写 UTF-8 标志，中文文件名会乱码），打包成功后删除散落 PDF

## 前置条件

- macOS GUI 会话；宿主进程（VS Code/终端）有 **辅助访问** + **屏幕录制** 权限
- CAD快速看图 5.2.3（「批量导出PDF」需会员）

## 关键坑（改脚本前必读）

- **Qt 的 AX 树不可靠**：`entire contents` 经常静默截断/返回空，所有控件查找必须用
  递归 handler（且 handler 内部要包 `tell application "System Events"`，否则 AX 术语
  编译不过）
- **不能用 keystroke 输入文字**：中文输入法会拦截按键（路径/文件名用 AX setValue）；
  方向键/回车等 key code 不受影响
- 会员弹出菜单是原始窗口，AX 读不出菜单项 → 只能键盘导航；**↓×18 = 批量导出PDF、
  ↓×17 = 导出PDF**（5.2.3 版本布局，升级后用截屏重新核对序号）
- sheet 的 AX 树挂载有延迟，点「导出」要重试循环
- AX setValue 后 UI 刷新有延迟，但值会生效
- 布局标题可能带尾随符号（如 "A2-A3 PLAN-"），以 AX 读到的原样为准
- 图框识别大图要 30 秒以上，等待上限给足（脚本现在 60s）
- AppleScript 源码放在 shell 单引号变量里时，不能出现 `AppleScript's`（撇号会截断字符串）

## 何时不用本 skill

服务端/无头/超大批量 → Docker 里的 ODAFileConverter（同内核、有真 CLI）。
