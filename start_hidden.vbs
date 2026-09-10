' 学习工作台 · 隐藏启动器（无 cmd 窗口）
' 双击后 python 在后台跑，不开任何 cmd 窗口，不会被误关
Option Explicit
Dim shell, cmd, base
base = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
cmd = Chr(34) & base & "\.venv\Scripts\python.exe" & Chr(34) & " " & Chr(34) & base & "\app.py" & Chr(34)
Set shell = CreateObject("WScript.Shell")
' 0 = 隐藏窗口, False = 不等待（让脚本立即退出）
shell.Run cmd, 0, False
Set shell = Nothing
