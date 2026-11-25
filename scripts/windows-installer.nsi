Unicode true
Name "VtoH"
OutFile "..\dist\VtoH-Setup-x64.exe"
InstallDir "$LOCALAPPDATA\Programs\VtoH"
RequestExecutionLevel user
ShowInstDetails show

Page directory
Page instfiles
UninstPage uninstConfirm
UninstPage instfiles

Section
  SetOutPath "$INSTDIR"
  File /r "..\dist\windows\VtoH\*"
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  CreateDirectory "$SMPROGRAMS\VtoH"
  CreateShortcut "$SMPROGRAMS\VtoH\VtoH.lnk" "$INSTDIR\VtoH.exe"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\VtoH" "DisplayName" "VtoH"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\VtoH" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\VtoH" "DisplayIcon" "$INSTDIR\VtoH.exe"
SectionEnd

Section "Uninstall"
  Delete "$SMPROGRAMS\VtoH\VtoH.lnk"
  RMDir "$SMPROGRAMS\VtoH"
  Delete "$INSTDIR\VtoH.exe"
  Delete "$INSTDIR\vtoh_capture.exe"
  RMDir /r "$INSTDIR\_internal"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\VtoH"
SectionEnd
