; 日历备忘录 —— NSIS 安装包脚本
;
; 为什么直接写 .nsi 而不用 `tauri build --bundles nsis`：
;   本文档的 Windows 产物是在 Linux 上交叉编译的（x86_64-pc-windows-gnu），
;   Tauri 的 NSIS bundler 依赖它自己的工具链与模板拉取（走 GitHub），
;   在交叉编译场景下不可靠。直接用 makensis + 自写脚本更可控、可复现。
;
; 产物：日历备忘录-<版本>-安装包.exe
; 用 makensis /DSRCDIR=<dir> /DOUTFILE=<file> /DPRODUCT_VERSION=1.0.0 install.nsi 构建

Unicode true
SetCompressor /SOLID lzma

; ── 可在命令行覆盖的宏 ──
!ifndef SRCDIR
  !define SRCDIR "..\app"
!endif
!ifndef OUTFILE
  !define OUTFILE "日历备忘录-安装包.exe"
!endif
!ifndef PRODUCT_VERSION
  !define PRODUCT_VERSION "1.0.0"
!endif
!ifndef ICONFILE
  !define ICONFILE "..\icons\icon.ico"
!endif

!define PRODUCT_NAME "日历备忘录"
!define PRODUCT_PUBLISHER "本地工具"
!define PRODUCT_ID "cn.local.memo"

Name "${PRODUCT_NAME}"
OutFile "${OUTFILE}"
InstallDir "$LOCALAPPDATA\${PRODUCT_ID}"
InstallDirRegKey HKCU "Software\${PRODUCT_ID}" "InstallDir"
RequestExecutionLevel user          ; 装到 %LOCALAPPDATA%，不需要管理员
ShowInstDetails show
ShowUninstDetails show

VIProductVersion "${PRODUCT_VERSION}.0"
VIAddVersionKey "ProductName" "${PRODUCT_NAME}"
VIAddVersionKey "ProductVersion" "${PRODUCT_VERSION}"
VIAddVersionKey "FileDescription" "${PRODUCT_NAME} 安装程序"
VIAddVersionKey "FileVersion" "${PRODUCT_VERSION}"
VIAddVersionKey "LegalCopyright" ""

; ── 界面 ──
!include "MUI2.nsh"
!define MUI_ABORTWARNING
!define MUI_ICON "${ICONFILE}"
!define MUI_UNICON "${ICONFILE}"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_RUN "$INSTDIR\memo-desktop.exe"
!define MUI_FINISHPAGE_RUN_TEXT "立即启动 ${PRODUCT_NAME}"
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "SimpChinese"

; ── 卸载时保留用户数据 ──
; data 目录含 memo.db（全部日程）。卸载默认不删，避免误删用户多年数据；
; 用户主动勾选才删。这是有意的保守选择。
Section "主程序" SecMain
  SetOutPath "$INSTDIR"

  ; 升级安装时旧文件可能仍被上一版进程占用，先杀进程再覆盖。
  ; 不预清理会导致 File 覆盖静默失败（装完发现主程序还是旧的或缺失）。
  ExecWait 'taskkill /F /IM memo-desktop.exe /T' $0
  ExecWait 'taskkill /F /IM memo-server.exe /T' $0
  Sleep 1200
  Delete "$INSTDIR\memo-desktop.exe"
  Delete "$INSTDIR\memo-server.exe"
  Delete "$INSTDIR\WebView2Loader.dll"
  Delete "$INSTDIR\uninstall.exe"

  ; 三个必需文件：主程序 / sidecar / WebView2 加载器
  File "${SRCDIR}\memo-desktop.exe"
  File "${SRCDIR}\memo-server.exe"
  File "${SRCDIR}\WebView2Loader.dll"

  ; 数据目录只建不覆盖（升级安装时保护已有数据）
  CreateDirectory "$INSTDIR\data"

  ; 开始菜单快捷方式
  CreateDirectory "$SMPROGRAMS\${PRODUCT_NAME}"
  CreateShortCut "$SMPROGRAMS\${PRODUCT_NAME}\${PRODUCT_NAME}.lnk" "$INSTDIR\memo-desktop.exe" "" "$INSTDIR\memo-desktop.exe" 0
  CreateShortCut "$SMPROGRAMS\${PRODUCT_NAME}\卸载 ${PRODUCT_NAME}.lnk" "$INSTDIR\uninstall.exe"

  ; 桌面快捷方式（可选，默认建）
  CreateShortCut "$DESKTOP\${PRODUCT_NAME}.lnk" "$INSTDIR\memo-desktop.exe" "" "$INSTDIR\memo-desktop.exe" 0

  ; 写入卸载信息与注册表（供控制面板卸载列表显示）
  WriteRegStr HKCU "Software\${PRODUCT_ID}" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_ID}" "DisplayName" "${PRODUCT_NAME}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_ID}" "DisplayVersion" "${PRODUCT_VERSION}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_ID}" "Publisher" "${PRODUCT_PUBLISHER}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_ID}" "UninstallString" "$INSTDIR\uninstall.exe"
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_ID}" "NoModify" 1
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_ID}" "NoRepair" 1

  WriteUninstaller "$INSTDIR\uninstall.exe"
SectionEnd

Section "Uninstall"
  ; 先结束进程。taskkill 返回后进程退出与句柄释放不是瞬间完成的，
  ; 立即 Delete 会因文件仍被锁而静默失败（表现为卸载后主程序残留、
  ; 再次安装时 File 覆盖失败）。所以杀进程后必须等句柄真正释放。
  ExecWait 'taskkill /F /IM memo-desktop.exe /T' $0
  ExecWait 'taskkill /F /IM memo-server.exe /T' $0
  Sleep 1500

  ; 删除可能被锁的文件要重试：Delete 失败是高发点，不能只调一次
  !macro DELRETRY path
    Delete "${path}"
    IfFileExists "${path}" 0 +2
      Sleep 600
    Delete "${path}"
    IfFileExists "${path}" 0 +2
      Sleep 600
    Delete "${path}"
  !macroend

  !insertmacro DELRETRY "$INSTDIR\memo-desktop.exe"
  !insertmacro DELRETRY "$INSTDIR\memo-server.exe"
  !insertmacro DELRETRY "$INSTDIR\WebView2Loader.dll"
  !insertmacro DELRETRY "$INSTDIR\uninstall.exe"

  Delete "$SMPROGRAMS\${PRODUCT_NAME}\${PRODUCT_NAME}.lnk"
  Delete "$SMPROGRAMS\${PRODUCT_NAME}\卸载 ${PRODUCT_NAME}.lnk"
  RMDir "$SMPROGRAMS\${PRODUCT_NAME}"
  Delete "$DESKTOP\${PRODUCT_NAME}.lnk"

  ; 用户数据默认保留（含 memo.db）；仅当用户明确勾选才删
  IfSilent keep_data
  MessageBox MB_YESNO|MB_ICONQUESTION|MB_DEFBUTTON2 \
    "是否同时删除全部数据（日程、账号、备份）？$\r$\n选择「否」将保留数据，便于以后重装继续使用。" \
    IDNO keep_data
  RMDir /r "$INSTDIR\data"
  keep_data:

  RMDir "$INSTDIR"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_ID}"
  DeleteRegKey HKCU "Software\${PRODUCT_ID}"
SectionEnd
