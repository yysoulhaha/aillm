Set WshShell = CreateObject("WScript.Shell")
WshShell.Run Chr(34) & CreateObject("Scripting.FileSystemObject").GetAbsolutePathName("启动客户端.bat") & Chr(34), 0, False
