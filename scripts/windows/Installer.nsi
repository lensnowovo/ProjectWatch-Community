# -*- coding: utf-8 -*-
Unicode true
RequestExecutionLevel user
SetCompressor zlib
Name "ProjectWatch"
OutFile "${OUTPUT}"
InstallDir "${INSTALL_ROOT}"
InstallDirRegKey HKCU "${REG_KEY}" "InstallLocation"
!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "x64.nsh"
!include "WinVer.nsh"
!include "nsDialogs.nsh"
!include "FileFunc.nsh"
!define MUI_ABORTWARNING
!define MUI_ICON "${BUNDLE}\ProjectWatch.ico"
!define MUI_UNICON "${BUNDLE}\ProjectWatch.ico"
!define MUI_WELCOMEPAGE_TEXT "安装后无需 Python 或终端。首次打开只需配置自己的模型和搜索服务。$\r$\n$\r$\n升级前请从托盘退出 ProjectWatch。升级会备份资料，卸载不会删除资料、配置或备份。$\r$\n$\r$\n关闭浏览器不会停止监控；请使用托盘菜单安全退出。"
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "${BUNDLE}\LICENSE"
!insertmacro MUI_PAGE_COMPONENTS
!define MUI_DIRECTORYPAGE_TEXT_TOP "选择程序安装位置。Python 和依赖会一并安装，无需额外配置。$\r$\n$\r$\n资料与配置独立保存在：${DATA_HOME}$\r$\n升级沿用原安装位置；如需迁移程序，请先卸载后重新安装，资料会保留。"
!define MUI_PAGE_CUSTOMFUNCTION_LEAVE ValidateInstallLocation
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_TEXT "安装完成。请从开始菜单或桌面打开 ProjectWatch。$\r$\n$\r$\n关闭网页后监控仍在运行；退出请使用托盘菜单。资料与配置独立保存在用户目录。"
!insertmacro MUI_PAGE_FINISH
!define MUI_UNCONFIRMPAGE_TEXT_TOP "将注销 ProjectWatch 并移除快捷方式；下一步可选择保留程序目录。$\r$\n资料、配置、原件和升级备份始终保留。$\r$\n请先从托盘安全退出。"
!insertmacro MUI_UNPAGE_CONFIRM
UninstPage custom un.ProgramOptions un.ProgramOptionsLeave
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "SimpChinese"
Var Guard
Var PreviousInstallDir
Var KeepProgram
Var KeepProgramCheckbox
Var KeepProgramArgs

!macro GuardInstaller
  SetShellVarContext current
  System::Call 'kernel32::CreateMutexW(p 0, i 0, w "${MUTEX}") p .r0 ?e'
  Pop $1
  StrCpy $Guard $0
  ${If} $1 = 183
    MessageBox MB_OK "另一个安装或卸载操作正在进行。" /SD IDOK
    SetErrorLevel 2
    Quit
  ${EndIf}
!macroend

Function .onInit
  ${IfNot} ${RunningX64}
  ${OrIfNot} ${AtLeastWin10}
    MessageBox MB_OK "此安装包需要 Windows 10/11 x64。" /SD IDOK
    SetErrorLevel 1
    Quit
  ${EndIf}
  !insertmacro GuardInstaller
FunctionEnd
Function un.onInit
  !insertmacro GuardInstaller
  StrCpy $KeepProgram ${BST_CHECKED}
  ${GetParameters} $0
  ClearErrors
  ${GetOptions} $0 "/REMOVEFILES" $1
  ${IfNot} ${Errors}
    StrCpy $KeepProgram ${BST_UNCHECKED}
  ${EndIf}
FunctionEnd

Function un.ProgramOptions
  nsDialogs::Create 1018
  Pop $0
  ${NSD_CreateLabel} 0 0 100% 40u "资料与配置始终保留。若改过程序文件，建议保留整个程序目录；以后可由 Agent 协助整理。"
  Pop $0
  ${NSD_CreateCheckbox} 0 48u 100% 22u "保留程序目录（包括本地定制）"
  Pop $KeepProgramCheckbox
  ${NSD_SetState} $KeepProgramCheckbox $KeepProgram
  ${NSD_CreateLabel} 0 80u 100% 44u "取消勾选会按安装清单删除程序和共用运行时，包括修改过的已列入清单文件；清单外文件保留。"
  Pop $0
  nsDialogs::Show
FunctionEnd

Function un.ProgramOptionsLeave
  ${NSD_GetState} $KeepProgramCheckbox $KeepProgram
FunctionEnd

Function ValidateInstallLocation
  ; NSIS GetFullPathName can fail for a not-yet-created directory. Normalize
  ; lexically with Windows instead; the helper rejects links before filesystem I/O.
  System::Call 'kernel32::GetFullPathNameW(w "$INSTDIR", i ${NSIS_MAX_STRLEN}, w .r3, p 0) i .r4'
  ${If} $4 = 0
  ${OrIf} $4 >= ${NSIS_MAX_STRLEN}
    MessageBox MB_OK|MB_ICONSTOP "安装目录无效或路径过长，请选择较短的本地目录。" /SD IDOK
    SetErrorLevel 1
    Abort
  ${EndIf}
  StrCpy $INSTDIR $3
  ; A test installer must never escape its sealed destinations via /D or the UI.
  !ifdef TEST_ROOT
    StrLen $1 "${TEST_ROOT}\"
    StrCpy $2 "$INSTDIR\" $1
    ${If} $2 != "${TEST_ROOT}\"
      MessageBox MB_OK|MB_ICONSTOP "测试安装包只能安装到指定隔离目录。" /SD IDOK
      SetErrorLevel 1
      Abort
    ${EndIf}
  !endif
  ReadRegStr $PreviousInstallDir HKCU "${REG_KEY}" "InstallLocation"
  ${If} $PreviousInstallDir != ""
  ${AndIf} $PreviousInstallDir != $INSTDIR
    MessageBox MB_OK|MB_ICONSTOP "已有安装位于：$PreviousInstallDir$\r$\n请沿用该目录升级。若要更换位置，请先卸载后重新安装；资料和配置会保留。" /SD IDOK
    SetErrorLevel 1
    Abort
  ${EndIf}
FunctionEnd

Section "ProjectWatch（完整功能）" Core
  SectionIn RO
  Call ValidateInstallLocation
  InitPluginsDir
  ; Minimal, independent Python runs the helper, so every payload file can be moved into place.
  SetOutPath "$PLUGINSDIR\runtime"
  File "${BUNDLE}\runtime\*.exe"
  File "${BUNDLE}\runtime\*.dll"
  File "${BUNDLE}\runtime\*.pyd"
  File "${BUNDLE}\runtime\*.zip"
  File "${BUNDLE}\runtime\*._pth"
  SetOutPath "$PLUGINSDIR"
  File "${HELPER}"
  ; Extract on the program volume: verified files are then moved, never copied and re-read.
  StrCpy $0 999
  ClearErrors
  ExecWait '"$PLUGINSDIR\runtime\pythonw.exe" -I -B "$PLUGINSDIR\maintain.py" prepare --root "$INSTDIR" --home "${DATA_HOME}" --receipt "$PLUGINSDIR\prepare.json"' $0
  ${If} ${Errors}
  ${OrIf} $0 != 0
    MessageBox MB_OK|MB_ICONSTOP "安装目录尚未准备好。请退出 ProjectWatch，检查目录权限；若有上次中断留下的 .payload，请交给 Agent 检查，不要直接删除。现有资料保持不变。" /SD IDOK
    SetErrorLevel 1
    Abort
  ${EndIf}
  ; NSIS can silently omit files beyond MAX_PATH while reporting exit 0.
  ; Extended paths are needed during extraction, not just in the Python helper.
  SetOutPath "\\?\$INSTDIR\.payload"
  File /r /x runtimes "${BUNDLE}\*"
  !ifdef DSH_RUNTIME_ID
  ${IfNot} ${FileExists} "$INSTDIR\runtimes\${DSH_RUNTIME_ID}\.complete"
    SetOutPath "\\?\$INSTDIR\.payload\runtimes\${DSH_RUNTIME_ID}"
    File /r "${BUNDLE}\runtimes\${DSH_RUNTIME_ID}\*"
  ${EndIf}
  !endif
  !ifdef NODE_RUNTIME_ID
  ${IfNot} ${FileExists} "$INSTDIR\runtimes\${NODE_RUNTIME_ID}\.complete"
    SetOutPath "\\?\$INSTDIR\.payload\runtimes\${NODE_RUNTIME_ID}"
    File /r "${BUNDLE}\runtimes\${NODE_RUNTIME_ID}\*"
  ${EndIf}
  !endif
  DetailPrint "校验安装文件、检查运行状态并备份资料…"
  StrCpy $0 999
  ClearErrors
  ExecWait '"$PLUGINSDIR\runtime\pythonw.exe" -I -B "$PLUGINSDIR\maintain.py" install --payload "$INSTDIR\.payload" --root "$INSTDIR" --home "${DATA_HOME}" --receipt "$PLUGINSDIR\result.json"' $0
  IfErrors 0 +2
    StrCpy $0 998
  SetOutPath "$INSTDIR"
  ${If} $0 != 0
    ; Keep the remaining payload and sanitized helper receipt for diagnosis.
    CopyFiles /SILENT "$PLUGINSDIR\result.json" "$INSTDIR\installer-result.json"
    MessageBox MB_OK|MB_ICONSTOP "未完成安装。失败回执 installer-result.json 和剩余安装文件已保留在程序目录，请交给维护者检查，不要直接删除。旧数据未删除，不会重试模型或搜索请求。" /SD IDOK
    SetErrorLevel 1
    Abort
  ${EndIf}
  ; Only a successful install may discard its own remaining extraction.
  RMDir /r "\\?\$INSTDIR\.payload"
  ClearErrors
  SetOutPath "$INSTDIR"
  File "${BUNDLE}\ProjectWatch.ico"
  File /oname=NSIS-COPYING.txt "${NSISDIR}\COPYING"
  ClearErrors
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  CreateDirectory "${MENU_DIR}"
  CreateShortcut "${MENU_DIR}\ProjectWatch.lnk" "$INSTDIR\ProjectWatch.exe" "" "$INSTDIR\ProjectWatch.ico" 0
  CreateShortcut "${MENU_DIR}\卸载 ProjectWatch.lnk" "$INSTDIR\Uninstall.exe" "" "$INSTDIR\ProjectWatch.ico" 0
  WriteRegStr HKCU "${REG_KEY}" "DisplayName" "ProjectWatch ${VERSION}"
  WriteRegStr HKCU "${REG_KEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${REG_KEY}" "Publisher" "qzqzqz"
  WriteRegStr HKCU "${REG_KEY}" "UninstallString" '$\"$INSTDIR\Uninstall.exe$\"'
  WriteRegStr HKCU "${REG_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${REG_KEY}" "DisplayIcon" "$INSTDIR\ProjectWatch.ico"
  WriteRegDWORD HKCU "${REG_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${REG_KEY}" "NoRepair" 1
  ${If} ${Errors}
    MessageBox MB_OK|MB_ICONEXCLAMATION "程序已安装，但快捷方式或卸载登记未完成。请重新运行安装器修复。资料保留。" /SD IDOK
    SetErrorLevel 1
    Abort
  ${EndIf}
SectionEnd

Section "创建桌面快捷方式" Desktop
  CreateDirectory "${DESKTOP_DIR}"
  CreateShortcut "${DESKTOP_DIR}\ProjectWatch.lnk" "$INSTDIR\ProjectWatch.exe" "" "$INSTDIR\ProjectWatch.ico" 0
SectionEnd

Section "Uninstall"
  InitPluginsDir
  SetOutPath "$PLUGINSDIR\runtime"
  ; Minimal, independent Python runtime: never execute files being removed.
  File "${BUNDLE}\runtime\*.exe"
  File "${BUNDLE}\runtime\*.dll"
  File "${BUNDLE}\runtime\*.pyd"
  File "${BUNDLE}\runtime\*.zip"
  File "${BUNDLE}\runtime\*._pth"
  SetOutPath "$PLUGINSDIR"
  File "${HELPER}"
  StrCpy $0 999
  StrCpy $KeepProgramArgs ""
  ${If} $KeepProgram = ${BST_CHECKED}
    StrCpy $KeepProgramArgs "--keep-program"
  ${EndIf}
  ClearErrors
  ExecWait '"$PLUGINSDIR\runtime\pythonw.exe" -I -B "$PLUGINSDIR\maintain.py" uninstall --root "$INSTDIR" --home "${DATA_HOME}" --receipt "$PLUGINSDIR\result.json" $KeepProgramArgs' $0
  ${If} ${Errors}
  ${OrIf} $0 != 0
    MessageBox MB_OK|MB_ICONSTOP "未完成卸载。请先从托盘退出 ProjectWatch；若仍失败请联系维护者。资料保持不变。" /SD IDOK
    SetErrorLevel 1
    Abort
  ${EndIf}
  Delete "${MENU_DIR}\ProjectWatch.lnk"
  Delete "${MENU_DIR}\卸载 ProjectWatch.lnk"
  RMDir "${MENU_DIR}"
  Delete "${DESKTOP_DIR}\ProjectWatch.lnk"
  DeleteRegKey HKCU "${REG_KEY}"
  ${If} $KeepProgram = ${BST_CHECKED}
    SetErrorLevel 0
    Return
  ${EndIf}
  Delete "$INSTDIR\Uninstall.exe"
  Delete "$INSTDIR\NSIS-COPYING.txt"
  Delete "$INSTDIR\ProjectWatch.ico"
  Delete "$INSTDIR\.studio.lock"
  ; Keep a receipt if customized files remain. Never RMDir /r.
  RMDir "$INSTDIR\versions"
  IfFileExists "$INSTDIR\versions\*" keep_receipt
    Delete "$INSTDIR\uninstalled.json"
    Delete "$INSTDIR\install-owner.json"
    RMDir "$INSTDIR"
  keep_receipt:
  SetErrorLevel 0
SectionEnd
