' Запуск Eve's Text без окна консоли.
Set fso = CreateObject("Scripting.FileSystemObject")
base = fso.GetParentFolderName(WScript.ScriptFullName)
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = base
sh.Run """" & base & "\bin\pythonw.exe"" """ & base & "\app\launcher.py""", 0, False
