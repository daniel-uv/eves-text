' Запуск Eve's Text без окна консоли.
' Предпочитаем нативный GUI-лагер: его можно связать с типами файлов.
Set fso = CreateObject("Scripting.FileSystemObject")
base = fso.GetParentFolderName(WScript.ScriptFullName)
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = base
launcher = base & "\Eve's Text.exe"
If fso.FileExists(launcher) Then
    sh.Run """" & launcher & """", 0, False
Else
    sh.Run """" & base & "\bin\pythonw.exe"" """ & base & "\app\launcher.py""", 0, False
End If
