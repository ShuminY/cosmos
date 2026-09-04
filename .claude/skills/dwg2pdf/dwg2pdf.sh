#!/bin/bash
# dwg2pdf.sh — 用 CAD快速看图.app 将 DWG 的每个布局导出为 PDF（GUI 自动化，实测于 5.2.3）
# 用法: dwg2pdf.sh <输入.dwg> [更多.dwg ...]
# 输出: 每张 dwg 归档为同目录 <dwg文件名>.zip（内含各布局 PDF）；无图框的布局自动跳过
#       KEEP_PDF=1 可在归档后保留散落的 PDF
set -euo pipefail

APP="CAD快速看图"
APP_PATH="/Applications/${APP}.app"
[ -d "$APP_PATH" ] || { echo "错误: 未安装 $APP_PATH"; exit 1; }
[ $# -ge 1 ] || { echo "用法: dwg2pdf.sh <输入.dwg> [更多.dwg ...]"; exit 1; }

# ---------- 通用清理：关掉残留的保存面板 / 批量导出sheet / 完成提示 ----------
cleanup() {
  osascript <<'ASA' >/dev/null 2>&1 || true
tell application "System Events"
	tell process "CAD快速看图"
		set frontmost to true
		delay 0.5
		repeat with i from 1 to 8
			set acted to false
			try
				key code 53
				set acted to true
				delay 1
			end try
			try
				repeat with w in windows
					try
						repeat with sh in sheets of w
							try
								click button "关闭" of sh
								set acted to true
								delay 1.5
							end try
						end repeat
					end try
					try
						if (count of buttons of w) ≥ 1 then
							click button "关闭" of w
							set acted to true
							delay 1.5
						end if
					end try
				end repeat
			end try
			if not acted then exit repeat
		end repeat
	end tell
end tell
ASA
}

# ---------- 递归收集 radio button 标题（entire contents 对 Qt 不可靠，必须手动递归） ----------
READ_TABS_AS='
on findRadios(theElem, theDepth)
	tell application "System Events"
		set acc to {}
		try
			if (role of theElem) is "AXRadioButton" then
				set theTitle to title of theElem
				if theTitle is not missing value then set end of acc to theTitle
			end if
		end try
		if theDepth < 8 then
			try
				set kids to UI elements of theElem
				repeat with theKid in kids
					set acc to acc & my findRadios(theKid, theDepth + 1)
				end repeat
			end try
		end if
		return acc
	end tell
end findRadios
tell application "System Events"
	tell process "CAD快速看图"
		set allRadios to my findRadios(window 1, 0)
		set text item delimiters of AppleScript to ","
		return allRadios as string
	end tell
end tell
'
read_tabs() { osascript -e "$READ_TABS_AS" 2>/dev/null || true; }

# 过滤出布局标签：文件标签页是 radio 但标题带前导空格/含.dwg，排除
layout_tabs() {
  echo "$1" | tr ',' '\n' | grep -v '^\ ' | grep -v '\.dwg' || true
}

# ---------- 点击指定布局标签（递归搜索） ----------
click_tab() {
  local TAB="$1"
  osascript <<ASA 2>/dev/null
on findRadio(theElem, theDepth, nm)
	tell application "System Events"
		if theDepth < 8 then
			try
				set kids to UI elements of theElem
				repeat with theKid in kids
					try
						if (role of theKid) is "AXRadioButton" and (title of theKid) is nm then
							click theKid
							return "ok"
						end if
					end try
					set r to my findRadio(theKid, theDepth + 1, nm)
					if r is "ok" then return "ok"
				end repeat
			end try
		end if
		return "fail"
	end tell
end findRadio
tell application "System Events"
	tell process "$APP"
		set frontmost to true
		delay 0.5
		return my findRadio(window 1, 0, "$TAB")
	end tell
end tell
ASA
}

# ---------- 等窗口标题包含指定文本 ----------
wait_window_title() {
  local needle="$1" timeout="${2:-90}"
  for i in $(seq 1 "$timeout"); do
    osascript -e "tell application \"System Events\" to tell process \"$APP\" to name of window 1" 2>/dev/null | grep -qF "$needle" && return 0
    sleep 1
  done
  return 1
}

# ---------- 打开批量导出面板（会员菜单第18项，键盘导航） ----------
open_batch_dialog() {
  osascript <<'ASA' >/dev/null 2>&1
tell application "System Events"
	tell process "CAD快速看图"
		set frontmost to true
		delay 1
		perform action "AXShowMenu" of pop up button "会员" of window 1
		delay 1
		repeat 18 times
			key code 125
			delay 0.05
		end repeat
		delay 0.3
		key code 36
	end tell
end tell
ASA
  for i in $(seq 1 30); do
    C=$(osascript -e "tell application \"System Events\" to tell process \"$APP\" to count sheets of window 1" 2>/dev/null || echo 0)
    [ "${C:-0}" -ge 1 ] && return 0
    sleep 1
  done
  return 1
}

# ---------- 读图框数（sheet 里形如 "N/M" 的文本，取 M；识别中则等待，最多60秒） ----------
# 注意：entire contents 对 Qt 不可靠（会静默截断），必须递归
wait_frames() {
  osascript <<'ASA' 2>/dev/null
on findFrames(theElem, theDepth)
	tell application "System Events"
		set acc to {}
		if theDepth < 8 then
			try
				if (role of theElem) is "AXStaticText" then
					set v to value of theElem
					if v is not missing value and v contains "/" then
						try
							set text item delimiters of AppleScript to "/"
							set end of acc to ((last text item of v) as integer)
						end try
					end if
				end if
			end try
			try
				set kids to UI elements of theElem
				repeat with theKid in kids
					set acc to acc & my findFrames(theKid, theDepth + 1)
				end repeat
			end try
		end if
		return acc
	end tell
end findFrames
tell application "System Events"
	tell process "CAD快速看图"
		set cnt to 0
		repeat with i from 1 to 60
			set vals to my findFrames(sheet 1 of window 1, 0)
			if (count of vals) > 0 then
				set cnt to last item of vals
				exit repeat
			end if
			delay 1
		end repeat
		return cnt as string
	end tell
end tell
ASA
}

# ---------- 导出当前布局；$1=输出pdf绝对路径 $2=保存面板文件名 ----------
export_current_layout() {
  local OUT_ABS="$1" SAVE_NAME="$2"
  local OUT_DIR OUT_TMP S1 S2
  OUT_DIR="$(dirname "$OUT_ABS")"
  OUT_TMP="${OUT_DIR}/${SAVE_NAME}.pdf"
  [ -e "$OUT_TMP" ] && mv "$OUT_TMP" "${OUT_TMP}.bak.$$"

  osascript <<ASA >/dev/null 2>&1
tell application "System Events"
	tell process "$APP"
		set frontmost to true
		delay 0.5
		set clicked to false
		repeat with i from 1 to 30
			try
				click button "导出" of sheet 1 of window 1
				set clicked to true
				exit repeat
			end try
			delay 1
		end repeat
		if not clicked then error "导出按钮30秒未就绪"
		delay 2
		-- 保存面板有两种形态：
		--   折叠态：按钮/文本框是窗口直接子元素（macOS 默认 / 非首次使用态）
		--   展开态：包在 splitter group 里
		set saveWin to missing value
		set nameField to missing value
		set saveBtn to missing value
		repeat with w in windows
			try
				set bFound to false
				set nf to missing value
				set sb to missing value
				-- 优先尝试展开态（splitter group）
				try
					set sg to splitter group 1 of w
					set nf to text field 1 of sg
					repeat with b in buttons of sg
						set bt to title of b
						if bt is "保存" or bt is "存储" or bt is "Save" then
							set sb to b
							exit repeat
						end if
					end repeat
				on error
					-- 折叠态：直接找窗口里的文本框和按钮
					try
						set nf to text field 1 of w
					end try
					repeat with b in buttons of w
						set bt to title of b
						if bt is "保存" or bt is "存储" or bt is "Save" then
							set sb to b
							exit repeat
						end if
					end repeat
				end try
				if sb is not missing value and nf is not missing value then
					set saveWin to w
					set nameField to nf
					set saveBtn to sb
					exit repeat
				end if
			end try
		end repeat
		if saveWin is missing value then error "未找到保存面板"
		set value of nameField to "$SAVE_NAME"
		delay 0.5
		click saveBtn
	end tell
end tell
ASA

  local DONE=0
  for i in $(seq 1 180); do
    if [ -e "$OUT_TMP" ]; then
      S1=$(stat -f%z "$OUT_TMP"); sleep 3; S2=$(stat -f%z "$OUT_TMP")
      [ "$S1" = "$S2" ] && [ "$S1" -gt 0 ] && { DONE=1; break; }
    fi
    sleep 1
  done
  [ "$DONE" = 1 ] || { echo "   ✖ 错误: 未生成 $OUT_TMP" >&2; return 1; }
  mv -f "$OUT_TMP" "$OUT_ABS"
  echo "   ✔ $(basename "$OUT_ABS") ($(du -h "$OUT_ABS" | cut -f1 | tr -d ' '))"
}

# ================= 主流程 =================
TOTAL_OK=0; TOTAL_SKIP=0

for DWG in "$@"; do
  [ -f "$DWG" ] || { echo "跳过（不存在）: $DWG"; continue; }
  DWG_ABS="$(cd "$(dirname "$DWG")" && pwd)/$(basename "$DWG")"
  BASE="$(basename "${DWG%.*}")"
  OUT_DIR="$(dirname "$DWG_ABS")"

  echo "==== $BASE ===="
  cleanup

  open -a "$APP_PATH" "$DWG_ABS"
  wait_window_title "$BASE" 90 || { echo "错误: 图纸加载超时"; continue; }
  sleep 5  # 等布局标签渲染

  TABS_RAW=$(read_tabs)
  LAYOUTS=$(layout_tabs "$TABS_RAW")
  if [ -z "$LAYOUTS" ]; then echo "错误: 未读到布局标签"; continue; fi
  echo ">> 布局: $(echo "$LAYOUTS" | tr '\n' ' ')"

  GEN_PDFS=()
  while IFS= read -r TAB; do
    [ -n "$TAB" ] || continue
    echo ">> 布局 [$TAB]"

    cleanup  # 切布局前清残留

    [ "$(click_tab "$TAB")" = "ok" ] || { echo "   ✖ 无法点击布局标签"; continue; }
    sleep 4  # 等布局渲染

    open_batch_dialog || open_batch_dialog || { echo "   ✖ 批量导出面板未出现"; continue; }

    FRAMES=$(wait_frames)
    if [ "${FRAMES:-0}" -eq 0 ]; then
      echo "   – 无图框，跳过"
      cleanup
      TOTAL_SKIP=$((TOTAL_SKIP+1))
      continue
    fi
    echo "   图框数: $FRAMES"

    SAFE_TAB=$(echo "$TAB" | tr '/' '_')
    if export_current_layout "${OUT_DIR}/${BASE}_${SAFE_TAB}.pdf" "${BASE}_${SAFE_TAB}"; then
      GEN_PDFS+=("${OUT_DIR}/${BASE}_${SAFE_TAB}.pdf")
      TOTAL_OK=$((TOTAL_OK+1))
    fi

    cleanup  # 关完成提示 + 批量面板
  done <<< "$LAYOUTS"

  # ---- 归档：本张 dwg 的所有布局 PDF 打包为 <dwg文件名>.zip ----
  if [ ${#GEN_PDFS[@]} -gt 0 ]; then
    ZIP_PATH="${OUT_DIR}/${BASE}.zip"
    STAGE=$(mktemp -d)
    for PDF in "${GEN_PDFS[@]}"; do cp "$PDF" "$STAGE/"; done
    # 用 python3 zipfile 打包：非 ASCII 文件名自动置 UTF-8 标志（系统 zip 命令不写，会乱码）
    if python3 -c "
import zipfile, os, sys
stage, out = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(out + '.tmp', 'w', zipfile.ZIP_DEFLATED) as z:
    for n in sorted(os.listdir(stage)):
        z.write(os.path.join(stage, n), n)
" "$STAGE" "$ZIP_PATH" 2>/dev/null; then
      mv -f "$ZIP_PATH.tmp" "$ZIP_PATH"
      # 打包成功后删除散落的 PDF（KEEP_PDF=1 可保留）
      [ "${KEEP_PDF:-0}" = "1" ] || rm -f "${GEN_PDFS[@]}"
      echo ">> 已归档: $(basename "$ZIP_PATH") (${#GEN_PDFS[@]} 个PDF, $(du -h "$ZIP_PATH" | cut -f1 | tr -d ' '))"
    else
      rm -f "$ZIP_PATH.tmp"
      echo ">> ⚠ 归档失败，保留散落 PDF"
    fi
    rm -rf "$STAGE"
  fi
done

cleanup
echo "==== 完成: 成功 $TOTAL_OK 个, 跳过(无图框) $TOTAL_SKIP 个 ===="
