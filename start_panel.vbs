' Start the phone-pc-mirror control panel with no console window.
' Put a shortcut to this file in shell:startup to run it at logon.
Set sh = CreateObject("WScript.Shell")
dir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir
sh.Run "pythonw """ & dir & "\panel.py""", 0, False
